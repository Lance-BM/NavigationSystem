// Initialize map
var map = L.map('map').setView([14.638, 121.076], 15);
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; OpenStreetMap'
}).addTo(map);

// Draw route on map
function drawRouteOnMap(coords) {
    if (!coords || coords.length === 0) return;
    L.polyline(coords, { color: 'blue', weight: 3 }).addTo(map);
    map.fitBounds(coords);
}

// Form submission
document.getElementById("routeForm").addEventListener("submit", function(e) {
    e.preventDefault();
    const startName = document.getElementById("start").value;
    const endName = document.getElementById("end").value;
    geocodeAndRoute(startName, endName);
});

// Geocode + send to backend
function geocodeAndRoute(startName, endName) {
    Promise.all([
        fetch(`https://nominatim.openstreetmap.org/search?format=json&q=${encodeURIComponent(startName)}`)
            .then(res => res.json()),
        fetch(`https://nominatim.openstreetmap.org/search?format=json&q=${encodeURIComponent(endName)}`)
            .then(res => res.json())
    ]).then(results => {
        const startData = results[0][0];
        const endData = results[1][0];

        if (!startData) return alert("Start location not found");
        if (!endData) return alert("End location not found");

        const startLat = parseFloat(startData.lat);
        const startLon = parseFloat(startData.lon);
        const endLat = parseFloat(endData.lat);
        const endLon = parseFloat(endData.lon);

        // Add markers
        L.marker([startLat, startLon]).addTo(map).bindPopup("Start").openPopup();
        L.marker([endLat, endLon]).addTo(map).bindPopup("End").openPopup();

        // Send to Flask backend
        fetch("/route", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                start: { lat: startLat, lng: startLon },
                end: { lat: endLat, lng: endLon }
            })
        })
        .then(async res => {
            const text = await res.text();
            try {
                return JSON.parse(text);
            } catch(e) {
                console.error("Response not JSON:", text);
                throw e;
            }
        })
        .then(data => {
            if (data.error) return alert("Error: " + data.error);
            drawRouteOnMap(data.route);
        })
        .catch(err => console.error("Request failed:", err));
    });
}