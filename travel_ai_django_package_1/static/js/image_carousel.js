// Image carousel (Google Places / Wikipedia / Unsplash)
// - Lazy-load: fetch image candidates only when user clicks prev/next
// - Works with server-rendered cards and dynamically rendered modal cards (event delegation)

(function(){
  const stateMap = new WeakMap();
  const respCache = new Map(); // key -> images array

  function elKey(el){
    const kind = el.dataset.kind || "";
    const pid = el.dataset.placeId || "";
    const name = el.dataset.name || "";
    const lat = el.dataset.lat || "";
    const lon = el.dataset.lon || "";
    const seed = el.dataset.seed || "";
    return [kind, pid, name, lat, lon, seed].join("|");
  }

  function buildUrl(el){
    const p = new URLSearchParams();
    const add = (k,v)=>{ if(v!==undefined && v!==null && String(v).trim()!=="") p.set(k, String(v)); };
    add("kind", el.dataset.kind);
    add("place_id", el.dataset.placeId);
    add("name", el.dataset.name);
    add("city", el.dataset.city);
    add("country", el.dataset.country);
    add("lat", el.dataset.lat);
    add("lon", el.dataset.lon);
    add("poi_type", el.dataset.poiType);
    add("seed", el.dataset.seed);
    add("w", el.dataset.w || "900");
    add("h", el.dataset.h || "600");
    return "/api/images?" + p.toString();
  }

  function setOverlay(el, item){
    const pill = el.querySelector(".carousel-pill");
    const credit = el.querySelector(".carousel-credit");
    if(pill) pill.textContent = (item && item.source) ? String(item.source).toUpperCase() : "";
    if(credit){
      credit.innerHTML = "";
      if(item && item.credit && item.credit.text){
        if(item.credit.href){
          const a = document.createElement("a");
          a.href = item.credit.href;
          a.target = "_blank";
          a.rel = "noopener noreferrer";
          a.textContent = item.credit.text;
          credit.appendChild(a);
        } else {
          credit.textContent = item.credit.text;
        }
      }
    }
  }

  function applyImage(el, images, idx){
    const img = el.querySelector("img.carousel-img") || el.querySelector("img");
    if(!img) return;
    if(!images || !images.length) return;
    idx = ((idx % images.length) + images.length) % images.length;
    const item = images[idx];
    if(item && item.url){
      img.src = item.url;
    }
    setOverlay(el, item);
    stateMap.set(el, { images, idx });
  }

  async function ensureLoaded(el){
    const key = elKey(el);
    if(respCache.has(key)) return respCache.get(key);
    // Mark loading to prevent spam clicks
    el.dataset.loading = "1";
    try{
      const res = await fetch(buildUrl(el), { method: "GET" });
      const data = await res.json();
      const images = (data && data.ok && Array.isArray(data.images)) ? data.images : [];
      respCache.set(key, images);
      return images;
    }catch(e){
      return [];
    }finally{
      delete el.dataset.loading;
    }
  }

  function currentIndexBySrc(images, src){
    if(!images || !images.length || !src) return 0;
    const idx = images.findIndex(x => (x && x.url) && String(x.url) === String(src));
    return idx >= 0 ? idx : 0;
  }

  async function move(el, dir){
    if(!el || el.dataset.loading === "1") return;
    const img = el.querySelector("img.carousel-img") || el.querySelector("img");
    const curSrc = img ? img.getAttribute("src") : "";

    const st = stateMap.get(el);
    let images = st ? st.images : null;
    let idx = st ? st.idx : null;

    if(!images){
      images = await ensureLoaded(el);
      // Seed idx by current image if possible
      idx = currentIndexBySrc(images, curSrc);
    }
    if(!images || !images.length) return;
    idx = (idx == null) ? 0 : idx;
    idx = idx + dir;
    applyImage(el, images, idx);
  }

  // Event delegation for dynamically rendered cards
  document.addEventListener("click", (e)=>{
    const btn = e.target.closest(".carousel-btn");
    if(!btn) return;
    const el = btn.closest(".img-carousel");
    if(!el) return;
    e.preventDefault();
    const dir = btn.classList.contains("next") ? 1 : -1;
    move(el, dir);
  });

  // Initialize overlays for existing carousels (optional)
  document.addEventListener("DOMContentLoaded", ()=>{
    document.querySelectorAll(".img-carousel").forEach(el => {
      // If template provided a data-source label, show it; otherwise keep empty
      const pill = el.querySelector(".carousel-pill");
      if(pill && !pill.textContent){
        const s = (el.dataset.source || "").trim();
        if(s) pill.textContent = s.toUpperCase();
      }
    });
  });
})();
