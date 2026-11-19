// Initialize map
const map = L.map("map").setView([14.638, 121.076], 15);
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: "&copy; OpenStreetMap contributors",
}).addTo(map);

// One layer group for everything we draw, so it can be cleared between searches
const routeLayer = L.layerGroup().addTo(map);

const form = document.getElementById("routeForm");
const submitButton = form.querySelector('button[type="submit"]');

/** fetch + JSON parsing with proper HTTP error handling */
async function fetchJson(url, options = {}) {
  const res = await fetch(url, options);
  const text = await res.text();

  let data;
  try {
    data = JSON.parse(text);
  } catch {
    console.error("Response not JSON:", text);
    throw new Error(Server returned an invalid response (HTTP ${res.status}).);
  }

  if (!res.ok) {
    throw new Error(data?.error ?? Request failed (HTTP ${res.status}).);
  }
  return data;
}

/** Look up a place name with Nominatim; returns { lat, lng } or null */
async function geocode(query, signal) {
  const params = new URLSearchParams({ format: "json", limit: "1", q: query });
  const results = await fetchJson(
    https://nominatim.openstreetmap.org/search?${params},
    { signal },
  );

  const hit = results?.[0];
  return hit ? { lat: Number(hit.lat), lng: Number(hit.lon) } : null;
}

function drawRouteOnMap(coords) {
  if (!coords?.length) return;
  L.polyline(coords, { color: "blue", weight: 3 }).addTo(routeLayer);
  map.fitBounds(coords);
}

function addMarker({ lat, lng }, label) {
  L.marker([lat, lng])
    .addTo(routeLayer)
    .bindTooltip(label, { permanent: true, direction: "top" });
}

let currentRequest = null;

async function geocodeAndRoute(startName, endName) {
  // Cancel any in-flight search
  currentRequest?.abort();
  currentRequest = new AbortController();
  const { signal } = currentRequest;

  submitButton.disabled = true;

  try {
    const [start, end] = await Promise.all([
      geocode(startName, signal),
      geocode(endName, signal),
    ]);

    if (!start) return alert("Start location not found");
    if (!end) return alert("End location not found");

    routeLayer.clearLayers();
    addMarker(start, "Start");
    addMarker(end, "End");

    const data = await fetchJson("/route", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ start, end }),
      signal,
    });

    drawRouteOnMap(data.route);
  } catch (err) {
    if (err.name === "AbortError") return;
    console.error("Request failed:", err);
    alert(Error: ${err.message});
  } finally {
    submitButton.disabled = false;
  }
}

form.addEventListener("submit", (e) => {
  e.preventDefault();
  const startName = document.getElementById("start").value.trim();
  const endName = document.getElementById("end").value.trim();
  if (!startName || !endName) return alert("Please enter both locations.");
  geocodeAndRoute(startName, endName);
}); 
