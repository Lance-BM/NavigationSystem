import logging
import importlib
import os
import sqlite3
from contextlib import contextmanager
from functools import wraps
from math import atan2, cos, radians, sin, sqrt
from pathlib import Path
from typing import Final, TypedDict

import networkx as nx
import osmnx as ox
import requests
from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash


app = Flask(__name__)


logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# OSMnx settings (osmnx 2.x)
# ---------------------------------------------------------------------------


ox.settings.use_cache = True
ox.settings.cache_folder = os.environ.get("OSMNX_CACHE", "cache")
# Seconds to wait for each Overpass server. Kept short so that trying several servers
# still finishes inside gunicorn's worker timeout (e.g. 3 servers x 30 s = 90 s).
ox.settings.requests_timeout = int(os.environ.get("OVERPASS_TIMEOUT", 30))
ox.settings.log_console = False


def _osmnx_error(name: str) -> type[Exception]:
    """Find an osmnx exception class.

    osmnx 2.x keeps its exceptions in the private module osmnx._errors (there is no ox.errors);
    osmnx 1.x used osmnx.errors. If neither has it, return a class that osmnx never raises,
    so such errors simply fall through to the generic handler.
    """
    for module_name in ("osmnx._errors", "osmnx.errors"):
        try:
            return getattr(importlib.import_module(module_name), name)
        except (ImportError, AttributeError):
            continue
    return type(name, (Exception,), {})


InsufficientResponseError = _osmnx_error("InsufficientResponseError")
ResponseStatusCodeError = _osmnx_error("ResponseStatusCodeError")


class MapDataUnavailable(Exception):
    """None of the Overpass (OpenStreetMap data) servers could be reached."""




class Coord(TypedDict):
    lat: float
    lng: float




type LatLon = list[float]  # [lat, lon] pair (PEP 695 type alias, Python 3.12+)


# ---------------------------------------------------------------------------
# Noise cost configuration
# ---------------------------------------------------------------------------


# Higher multiplier = noisier = less preferred by the routing algorithm.
# Anything not listed falls back to DEFAULT_NOISE_FACTOR.
HIGHWAY_NOISE_FACTORS: Final[dict[str, float]] = {
    # Very noisy
    "motorway": 8,
    "motorway_link": 8,
    "trunk": 6,
    "trunk_link": 6,
    "primary": 5,
    "primary_link": 5,
    # Moderate
    "secondary": 4,
    "secondary_link": 4,
    "tertiary": 3,
    "tertiary_link": 3,
    "unclassified": 2,
    # Quiet
    "residential": 1.2,
    "living_street": 1,
    "pedestrian": 1,
    "footway": 1,
    "path": 1,
    "cycleway": 1,
    "steps": 1,
    "corridor": 1,
    "track": 1.2,
    "service": 1.5,
}
DEFAULT_NOISE_FACTOR: Final = 2.5  # Sensible mid-range for unknown road types
EARTH_RADIUS_KM: Final = 6371.0
# Public Overpass servers (where the OpenStreetMap street data is downloaded from), tried in order.
# overpass-api.de is often overloaded or refuses connections, so mirrors are used as a fallback.
# Override with OVERPASS_URLS: comma-separated base URLs, each ending in /api.
DEFAULT_OVERPASS_URLS: Final = (
    "https://overpass-api.de/api",
    "https://overpass.private.coffee/api",
    "https://maps.mail.ru/osm/tools/overpass/api",
)
OVERPASS_URLS: Final = [
    url.strip().rstrip("/")
    for url in os.environ.get("OVERPASS_URLS", "").split(",")
    if url.strip()
] or list(DEFAULT_OVERPASS_URLS)
MAX_ROUTE_KM: Final = float(os.environ.get("MAX_ROUTE_KM", 15))  # guard against huge downloads




# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return the great-circle distance in kilometres between two coordinates."""
    lat1, lon1, lat2, lon2 = map(radians, (lat1, lon1, lat2, lon2))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return EARTH_RADIUS_KM * 2 * atan2(sqrt(a), sqrt(1 - a))




def graph_radius_m(distance_km: float, padding: float = 1.3, minimum_m: float = 1500) -> float:
    """
    Convert a crow-flies route distance into an OSM download radius.
    We add padding so the graph comfortably contains both endpoints,
    and enforce a minimum so very short routes still get a usable graph.
    """
    return max(minimum_m, (distance_km / 2) * 1000 * padding)




def add_noise_costs(G: nx.MultiDiGraph) -> nx.MultiDiGraph:
    """Annotate every edge with a 'cost' = length × noise_factor."""
    for _, _, data in G.edges(data=True):
        highway = data.get("highway", "")
        # OSM sometimes returns a list (multiple tags); take the first.
        if isinstance(highway, list):
            highway = highway[0] if highway else ""


        factor = HIGHWAY_NOISE_FACTORS.get(highway, DEFAULT_NOISE_FACTOR)
        data["cost"] = data.get("length", 1) * factor


    return G




def parse_coord(raw: object, field: str) -> Coord:
    """
    Validate and return a coordinate with 'lat' and 'lng' floats.
    Raises ValueError with a descriptive message on bad input.
    """
    if not isinstance(raw, dict):
        raise ValueError(f"'{field}' must be an object with 'lat' and 'lng' keys.")


    try:
        lat = float(raw["lat"])
        lng = float(raw["lng"])
    except (KeyError, TypeError, ValueError):
        raise ValueError(f"'{field}' must have numeric 'lat' and 'lng' values.") from None


    if not -90 <= lat <= 90:
        raise ValueError(f"'{field}.lat' must be between -90 and 90.")
    if not -180 <= lng <= 180:
        raise ValueError(f"'{field}.lng' must be between -180 and 180.")


    return Coord(lat=lat, lng=lng)




def build_graph(start: Coord, end: Coord) -> nx.MultiDiGraph:
    """Download the OSM walking graph sized to fit the requested route."""
    center_lat = (start["lat"] + end["lat"]) / 2
    center_lng = (start["lng"] + end["lng"]) / 2


    distance_km = haversine_km(start["lat"], start["lng"], end["lat"], end["lng"])
    radius_m = graph_radius_m(distance_km)


    log.info(
        "Downloading OSM graph: centre=(%.4f, %.4f), radius=%.0fm, route_dist=%.2fkm",
        center_lat, center_lng, radius_m, distance_km,
    )


    for url in OVERPASS_URLS:
        ox.settings.overpass_url = url
        try:
            return ox.graph_from_point(
                (center_lat, center_lng),
                dist=radius_m,
                network_type="walk",
                simplify=True,
                truncate_by_edge=True,
            )
        except (requests.exceptions.RequestException, ResponseStatusCodeError) as exc:
            log.warning(
                "Overpass server %s failed (%s: %.200s); trying the next one.",
                url, type(exc).__name__, exc,
            )

    raise MapDataUnavailable




def find_quiet_route(G: nx.MultiDiGraph, start: Coord, end: Coord) -> tuple[list[int], list[LatLon]]:
    """Return (node ids, [lat, lon] pairs) for the quietest path through G.


    nearest_nodes on an unprojected graph relies on scikit-learn (BallTree).
    """
    orig = ox.distance.nearest_nodes(G, X=start["lng"], Y=start["lat"])
    dest = ox.distance.nearest_nodes(G, X=end["lng"], Y=end["lat"])


    route_nodes = nx.shortest_path(G, orig, dest, weight="cost")
    coords = [[G.nodes[n]["y"], G.nodes[n]["x"]] for n in route_nodes]
    return route_nodes, coords




def route_length_m(G: nx.MultiDiGraph, route_nodes: list[int]) -> float:
    """Sum real edge lengths along the route, using the cheapest parallel edge
    (the same one shortest_path picked) between each node pair."""
    total = 0.0
    for u, v in zip(route_nodes, route_nodes[1:]):
        best = min(G[u][v].values(), key=lambda d: d.get("cost", float("inf")))
        total += best.get("length", 0.0)
    return total



# ---------------------------------------------------------------------------
# Accounts & sessions
# ---------------------------------------------------------------------------


DEFAULT_SECRET_KEY: Final = "dev-only-secret-change-me"
# An empty or blank SECRET_KEY counts as "not set" (Flask would otherwise have no session key at all).
app.secret_key = os.environ.get("SECRET_KEY", "").strip() or DEFAULT_SECRET_KEY
app.config.update(SESSION_COOKIE_SAMESITE="Lax")
if app.secret_key == DEFAULT_SECRET_KEY:
    log.warning("SECRET_KEY is not set; using an insecure development key. Set SECRET_KEY in production.")

# Where user accounts are stored (SQLite). Override with the USERS_DB environment variable.
DB_PATH: Final = os.environ.get("USERS_DB", str(Path(__file__).with_name("users.db")))

@contextmanager
def db():
    """Open the SQLite database, commit on success, and always close the connection."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()

def init_db() -> None:
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL
            )
            """
        )


init_db()  # Runs at import time so the table also exists when started with gunicorn.


def login_required(view):
    """Send visitors who are not logged in back to the entry page."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "username" not in session:
            return redirect(url_for("landing"))
        return view(*args, **kwargs)
    return wrapped



# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/")

def landing():
    """Entry page: ask the visitor whether they want to log in or sign up."""
    if "username" in session:
        return redirect(url_for("map_page"))
    return render_template("landing.html")

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if "username" in session:
        return redirect(url_for("map_page"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if not username or not password:
            flash("Please enter a username and a password.")
        else:
            try:
                with db() as conn:
                    conn.execute(
                        "INSERT INTO users (username, password) VALUES (?, ?)",
                        (username, generate_password_hash(password)),
                    )
            except sqlite3.IntegrityError:
                flash("Username already exists. Try another.")
            else:
                # Account created: log the new user in and take them to the map.
                session.clear()
                session["username"] = username
                return redirect(url_for("map_page"))

    return render_template("signup.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if "username" in session:
        return redirect(url_for("map_page"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        with db() as conn:
            user = conn.execute(
                "SELECT username, password FROM users WHERE username = ?", (username,)
            ).fetchone()

        if user and check_password_hash(user["password"], password):
            session.clear()
            session["username"] = user["username"]
            return redirect(url_for("map_page"))
        flash("Invalid username or password.")

    return render_template("login.html")

@app.get("/map")
@login_required
def map_page():
    return render_template("map.html", username=session["username"])


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("landing"))




@app.get("/health")
def health():
    return jsonify(status="ok")




@app.post("/route")
def route():
    if "username" not in session:
        return jsonify(error="Please log in first."), 401

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify(error="Request body must be a JSON object."), 400


    try:
        start = parse_coord(payload.get("start"), "start")
        end = parse_coord(payload.get("end"), "end")
    except ValueError as exc:
        return jsonify(error=str(exc)), 400


    distance_km = haversine_km(start["lat"], start["lng"], end["lat"], end["lng"])
    if distance_km > MAX_ROUTE_KM:
        return jsonify(
            error=f"Route is {distance_km:.1f} km apart; the maximum supported is {MAX_ROUTE_KM:g} km."
        ), 400


    try:
        G = add_noise_costs(build_graph(start, end))
        route_nodes, coords = find_quiet_route(G, start, end)
    except nx.NetworkXNoPath:
        return jsonify(error="No walkable path found between those points."), 404
    except InsufficientResponseError:
        return jsonify(error="No OpenStreetMap walking data found for that area."), 404
    except MapDataUnavailable:
        return jsonify(
            error="The OpenStreetMap data service can't be reached right now. Please try again in a few minutes."
        ), 503
    except Exception:
        log.exception("Route computation failed")
        return jsonify(error="Failed to compute route."), 500


    return jsonify(
        route=coords,
        distance_m=round(route_length_m(G, route_nodes), 1),
        straight_line_km=round(distance_km, 3),
    )




if __name__ == "__main__":
    # Dev only. In production run: gunicorn -w 2 --timeout 120 app:app
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1", port=int(os.environ.get("PORT", 5000)))
