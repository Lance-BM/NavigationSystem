import logging
import os
from math import radians, sin, cos, sqrt, atan2

import networkx as nx
import osmnx as ox
from flask import Flask, jsonify, render_template, request

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Noise cost configuration
# ---------------------------------------------------------------------------

# Higher multiplier = noisier = less preferred by the routing algorithm.
# Anything not listed falls back to DEFAULT_FACTOR.
HIGHWAY_NOISE_FACTORS: dict[str, float] = {
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
    "track": 1.2,
    "service": 1.5,
}
DEFAULT_NOISE_FACTOR = 2.5  # Sensible mid-range for unknown road types


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return the great-circle distance in kilometres between two coordinates."""
    R = 6371.0
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def graph_radius_m(distance_km: float, padding: float = 1.3, minimum_m: float = 1500) -> float:
    """
    Convert a crow-flies route distance into an OSM download radius.
    We add padding so the graph comfortably contains both endpoints,
    and enforce a minimum so very short routes still get a usable graph.
    """
    return max(minimum_m, (distance_km / 2) * 1000 * padding)


def add_noise_costs(G: nx.MultiDiGraph) -> nx.MultiDiGraph:
    """Annotate every edge with a 'cost' = length × noise_factor."""
    for u, v, k, data in G.edges(keys=True, data=True):
        highway = data.get("highway", "")
        # OSM sometimes returns a list (multiple tags); take the first.
        if isinstance(highway, list):
            highway = highway[0]

        factor = HIGHWAY_NOISE_FACTORS.get(highway, DEFAULT_NOISE_FACTOR)
        data["cost"] = data.get("length", 1) * factor

    return G


def parse_coord(raw: object, field: str) -> dict:
    """
    Validate and return a coordinate dict with 'lat' and 'lng' floats.
    Raises ValueError with a descriptive message on bad input.
    """
    if not isinstance(raw, dict):
        raise ValueError(f"'{field}' must be an object with 'lat' and 'lng' keys.")

    try:
        lat = float(raw["lat"])
        lng = float(raw["lng"])
    except (KeyError, TypeError, ValueError):
        raise ValueError(f"'{field}' must have numeric 'lat' and 'lng' values.")

    if not (-90 <= lat <= 90):
        raise ValueError(f"'{field}.lat' must be between -90 and 90.")
    if not (-180 <= lng <= 180):
        raise ValueError(f"'{field}.lng' must be between -180 and 180.")

    return {"lat": lat, "lng": lng}


def build_graph(start: dict, end: dict) -> nx.MultiDiGraph:
    """Download the OSM walking graph sized to fit the requested route."""
    center_lat = (start["lat"] + end["lat"]) / 2
    center_lng = (start["lng"] + end["lng"]) / 2

    distance_km = haversine_km(start["lat"], start["lng"], end["lat"], end["lng"])
    radius_m = graph_radius_m(distance_km)

    log.info(
        "Downloading OSM graph: centre=(%.4f, %.4f), radius=%.0fm, route_dist=%.2fkm",
        center_lat, center_lng, radius_m, distance_km,
    )

    return ox.graph_from_point(
        (center_lat, center_lng),
        dist=radius_m,
        network_type="walk",
        simplify=True,
        truncate_by_edge=True,
    )


def find_quiet_route(G: nx.MultiDiGraph, start: dict, end: dict) -> list[list[float]]:
    """Return a list of [lat, lon] pairs for the quietest path through G."""
    orig = ox.distance.nearest_nodes(G, X=start["lng"], Y=start["lat"])
    dest = ox.distance.nearest_nodes(G, X=end["lng"], Y=end["lat"])

    route_nodes = nx.shortest_path(G, orig, dest, weight="cost")
    return [[G.nodes[n]["y"], G.nodes[n]["x"]] for n in route_nodes]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/route", methods=["POST"])
def route():
    payload = request.get_json(silent=True)
    if not payload:
        return jsonify({"error": "Request body must be valid JSON."}), 400

    # Validate inputs before touching OSM.
    try:
        start = parse_coord(payload.get("start"), "start")
        end = parse_coord(payload.get("end"), "end")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 422

    try:
        G = build_graph(start, end)
        G = add_noise_costs(G)
        coords = find_quiet_route(G, start, end)
        return jsonify({"route": coords})

    except nx.NetworkXNoPath:
        return jsonify({"error": "No path found between those locations."}), 404
    except nx.NodeNotFound:
        return jsonify({"error": "One or both locations could not be found on the map."}), 404
    except Exception as exc:
        # Log the real error server-side; don't leak internals to the client.
        log.exception("Unexpected error during routing: %s", exc)
        return jsonify({"error": "An unexpected server error occurred."}), 500


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    app.run(debug=False)