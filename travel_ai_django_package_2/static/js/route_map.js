// Leaflet route render (Google지도 느낌의 인터랙티브 지도)
document.addEventListener("DOMContentLoaded", () => {
  const el = document.getElementById("leafletMap");
  if (!el) return;
  if (typeof L === "undefined") return;

  let routeLine = [];
  let nodes = [];
  try { routeLine = JSON.parse(el.dataset.route || "[]"); } catch(e) {}
  try { nodes = JSON.parse(el.dataset.nodes || "[]"); } catch(e) {}

  // routeLine = [{lat,lng}, ...] OR nodes = [{name,lat,lng}, ...]
  const latlngLine = (routeLine && routeLine.length >= 2)
    ? routeLine.map(p => [p.lat, p.lng])
    : (nodes && nodes.length >= 2 ? nodes.map(p => [p.lat, p.lng]) : []);

  if (!latlngLine || latlngLine.length < 2) return;

  const map = L.map(el, { zoomControl: true, scrollWheelZoom: false });
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "&copy; OpenStreetMap contributors"
  }).addTo(map);

  const line = L.polyline(latlngLine, { weight: 5 }).addTo(map);
  map.fitBounds(line.getBounds(), { padding: [20, 20] });

  // Numbered markers
  function numberedIcon(text, isHotel=false){
    const cls = isHotel ? "marker-hotel" : "marker-poi";
    return L.divIcon({
      className: cls,
      html: `<div class="marker-bubble">${text}</div>`,
      iconSize: [30, 30],
      iconAnchor: [15, 30]
    });
  }

  // start/end from nodes if available
  if (nodes && nodes.length >= 2) {
    nodes.forEach((p, idx) => {
      const isStart = idx === 0;
      const isEnd = idx === nodes.length - 1;
      const label = isStart ? "S" : (isEnd ? "E" : String(idx));
      const icon = numberedIcon(label, isHotel=(isStart||isEnd));
      L.marker([p.lat, p.lng], { icon }).addTo(map).bindPopup(p.name || "");
    });
  } else {
    // fallback markers on line ends
    L.marker(latlngLine[0], { icon: numberedIcon("S", true) }).addTo(map);
    L.marker(latlngLine[latlngLine.length-1], { icon: numberedIcon("E", true) }).addTo(map);
  }

  // marker styles
  const style = document.createElement("style");
  style.innerHTML = `
    .marker-poi .marker-bubble{
      width:28px;height:28px;border-radius:14px;
      display:flex;align-items:center;justify-content:center;
      background:#1a73e8;color:white;font-weight:700;
      border:2px solid rgba(255,255,255,.95);
      box-shadow: 0 4px 10px rgba(0,0,0,.25);
    }
    .marker-hotel .marker-bubble{
      width:28px;height:28px;border-radius:14px;
      display:flex;align-items:center;justify-content:center;
      background:#34a853;color:white;font-weight:800;
      border:2px solid rgba(255,255,255,.95);
      box-shadow: 0 4px 10px rgba(0,0,0,.25);
    }
  `;
  document.head.appendChild(style);
});
