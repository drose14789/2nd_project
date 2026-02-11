// Drag & Drop reorder for POIs + auto route recalculation
function getOrderFromList(listEl){
  const items = Array.from(listEl.querySelectorAll(".poi-sort-item"));
  return items.map(el => parseInt(el.dataset.idx, 10));
}

async function postJSON(url, payload){
  const res = await fetch(url, {
    method: "POST",
    headers: {"Content-Type":"application/json"},
    body: JSON.stringify(payload)
  });
  if(!res.ok){
    const txt = await res.text();
    throw new Error(txt || ("HTTP " + res.status));
  }
  return await res.json();
}

function renderOrderBadges(listEl){
  const items = Array.from(listEl.querySelectorAll(".poi-sort-item"));
  items.forEach((el, i) => {
    const badge = el.querySelector(".badge.bg-dark");
    if (badge) badge.textContent = String(i+1);
  });
}

function updateRouteList(route){
  const ul = document.querySelector(".route-list");
  if(!ul || !route) return;
  ul.innerHTML = "";
  route.forEach((r, i) => {
    const li = document.createElement("li");
    // Keep numeric ordering (1., 2., 3...) and keep existing list-group look.
    li.className = "list-group-item d-flex justify-content-between";
    const lat = (r && r.lat !== undefined) ? Number(r.lat).toFixed(3) : "";
    const lng = (r && r.lng !== undefined) ? Number(r.lng).toFixed(3) : "";
    li.innerHTML = `<span>${i+1}. ${r.name}</span><span class="small-note">${lat}${lat && lng ? ", " : ""}${lng}</span>`;
    ul.appendChild(li);
  });
}

function updateMapStatic(url){
  const img = document.querySelector(".route-map img");
  if(!img) return;
  img.src = url;
  img.style.display = "block";
  const wrap = document.getElementById("leafletWrap");
  if(wrap) wrap.style.display = "none";
}

function updateLeafletData(routeLine, nodes){
  const el = document.getElementById("leafletMap");
  if(!el) return;
  el.dataset.route = JSON.stringify(routeLine || []);
  el.dataset.nodes = JSON.stringify(nodes || []);
  // route_map.js listens only on DOMContentLoaded, so we trigger a hard refresh by reloading iframe-like logic:
  // simplest: reload page section by full reload
  // but to keep snappy: just reload the page once after recalculation
}
function redrawLeaflet(routeLine, nodes){
  const el = document.getElementById("leafletMap");
  if(!el) return;
  if(typeof L === "undefined") return;

  // If static map is visible, we don't need leaflet redraw
  const wrap = document.getElementById("leafletWrap");
  const img = document.querySelector(".route-map img");
  if(img && img.style.display !== "none") return;

  // Remove old instance
  if(window.__routeLeafletMap){
    try{ window.__routeLeafletMap.remove(); }catch(e){}
    window.__routeLeafletMap = null;
  }

  const latlngLine = (routeLine && routeLine.length >= 2)
    ? routeLine.map(p => [p.lat, p.lng])
    : (nodes && nodes.length >= 2 ? nodes.map(p => [p.lat, p.lng]) : []);

  if(latlngLine.length < 2) return;

  const map = L.map(el, { zoomControl: true, scrollWheelZoom: false });
  window.__routeLeafletMap = map;
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "&copy; OpenStreetMap contributors"
  }).addTo(map);

  const line = L.polyline(latlngLine, { weight: 5 }).addTo(map);
  map.fitBounds(line.getBounds(), { padding: [20,20] });
}


let _recalcTimer = null;
async function recalcNow(){
  // v26+ uses #poiSortable (older versions used #poiList)
  const listEl = document.getElementById("poiSortable") || document.getElementById("poiList");
  const dayEl = document.getElementById("globalDay");
  if(!listEl || !dayEl) return;

  const order = getOrderFromList(listEl);
  const globalDay = parseInt(dayEl.value, 10);

  try{
    const data = await postJSON("/api/reorder_day", {global_day: globalDay, order});
    // Update static map
    if (data.route_map_url) updateMapStatic(data.route_map_url);
    // Update route list
    if (data.route) updateRouteList(data.route);
    // Update time text
    const timeEl = document.getElementById("moveTimeText");
    if(timeEl && data.total_move_time) timeEl.textContent = data.total_move_time;
// Update leaflet dataset then reload to redraw properly
    updateLeafletData(data.route_line, data.route);
    // soft reload only map: simplest is reload page section
    // here we trigger a full reload to ensure everything syncs
    // window.location.reload();
  }catch(e){
    console.error(e);
    alert("루트 재계산 실패: " + e.message);
  }
}

function scheduleRecalc(){
  if(_recalcTimer) clearTimeout(_recalcTimer);
  _recalcTimer = setTimeout(recalcNow, 400);
}

function initDnD(){
  const listEl = document.getElementById("poiSortable") || document.getElementById("poiList");
  if(!listEl) return;

  let dragging = null;

  listEl.addEventListener("dragstart", (e) => {
    const item = e.target.closest(".poi-sort-item");
    if(!item) return;
    dragging = item;
    item.classList.add("dragging");
    e.dataTransfer.effectAllowed = "move";
  });

  listEl.addEventListener("dragend", (e) => {
    const item = e.target.closest(".poi-sort-item");
    if(item) item.classList.remove("dragging");
    dragging = null;
    renderOrderBadges(listEl);
    scheduleRecalc();
  });

  listEl.addEventListener("dragover", (e) => {
    e.preventDefault();
    const afterEl = getDragAfterElement(listEl, e.clientY);
    if(!dragging) return;
    if(afterEl == null){
      listEl.appendChild(dragging);
    }else{
      listEl.insertBefore(dragging, afterEl);
    }
  });
}

// Pointer-based reorder (works on touch devices and in environments where HTML5 drag is flaky)
function initPointerDnD(){
  const listEl = document.getElementById("poiSortable") || document.getElementById("poiList");
  if(!listEl) return;

  let dragging = null;
  let placeholder = null;
  let offsetY = 0;
  let pointerId = null;

  function cleanup(){
    if(!dragging) return;
    dragging.classList.remove("dragging");
    dragging.style.position = "";
    dragging.style.zIndex = "";
    dragging.style.left = "";
    dragging.style.top = "";
    dragging.style.width = "";
    if(placeholder){
      listEl.insertBefore(dragging, placeholder);
      placeholder.remove();
      placeholder = null;
    }
    dragging = null;
    pointerId = null;
    renderOrderBadges(listEl);
    scheduleRecalc();
  }

  listEl.addEventListener("pointerdown", (e) => {
    const handle = e.target.closest(".drag");
    if(!handle) return;
    const item = handle.closest(".poi-sort-item");
    if(!item) return;

    // Prevent scrolling while dragging on touch devices
    e.preventDefault();

    dragging = item;
    pointerId = e.pointerId;
    try{ dragging.setPointerCapture(pointerId); }catch(_){ }

    const rect = item.getBoundingClientRect();
    offsetY = e.clientY - rect.top;

    // placeholder
    placeholder = document.createElement("li");
    placeholder.className = "poi-sort-placeholder";
    placeholder.style.height = rect.height + "px";
    listEl.insertBefore(placeholder, item.nextSibling);

    item.classList.add("dragging");
    item.style.position = "fixed";
    item.style.zIndex = "9999";
    item.style.left = rect.left + "px";
    item.style.width = rect.width + "px";
    item.style.top = rect.top + "px";
  }, {passive:false});

  listEl.addEventListener("pointermove", (e) => {
    if(!dragging || (pointerId !== null && e.pointerId !== pointerId)) return;
    e.preventDefault();

    const y = e.clientY - offsetY;
    dragging.style.top = y + "px";

    // Decide placeholder position by current pointer Y
    const items = [...listEl.querySelectorAll(".poi-sort-item:not(.dragging)")];
    let inserted = false;
    for(const it of items){
      const r = it.getBoundingClientRect();
      const mid = r.top + r.height / 2;
      if(e.clientY < mid){
        listEl.insertBefore(placeholder, it);
        inserted = true;
        break;
      }
    }
    if(!inserted){
      listEl.appendChild(placeholder);
    }
  }, {passive:false});

  listEl.addEventListener("pointerup", cleanup);
  listEl.addEventListener("pointercancel", cleanup);
}

function getDragAfterElement(container, y){
  const els = [...container.querySelectorAll(".poi-sort-item:not(.dragging)")];
  return els.reduce((closest, child) => {
    const box = child.getBoundingClientRect();
    const offset = y - box.top - box.height / 2;
    if(offset < 0 && offset > closest.offset){
      return {offset, element: child};
    }else{
      return closest;
    }
  }, {offset: Number.NEGATIVE_INFINITY}).element;
}

document.addEventListener("DOMContentLoaded", () => {
  initDnD();
  initPointerDnD();
  const btn = document.getElementById("btnRecalcRoute");
  if(btn) btn.addEventListener("click", recalcNow);
});
