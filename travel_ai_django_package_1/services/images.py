"""
Unsplash Image Service (다양한 이미지/고해상도 지원)
- query 기반 검색 → seed(해시)로 결과를 고정/다양화
- 웹용/ PPT용(HD) URL 생성
"""
import os
import hashlib
import requests
import re
import json
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple
from urllib.parse import quote

from storage.place_cache import PlacesCache

_CACHE: Dict[str, Any] = {}

# =========================================================
# ✅ City/Country hero overrides (deterministic, landmark-like)
#    - Prevent random 'cafe/food' images for cities like Tokyo
#    - Uses Wikimedia Commons Special:FilePath (stable CDN)
# =========================================================
CITY_NAME_ALIASES = {
    # English / variants -> Korean canonical used by locations_kor.json
    "Seoul": "서울",
    "Busan": "부산",
    "Jeju": "제주",
    "Jeju Island": "제주",
    "Tokyo": "도쿄",
    "Osaka": "오사카",
    "Bangkok": "방콕",
    "Ho Chi Minh": "호치민",
    "Ho Chi Minh City": "호치민",
    "Da Nang": "다낭",
    "London": "런던",
    "Paris": "파리",
    "Amsterdam": "암스테르담",
    "Berlin": "베를린",
    "Munich": "뮌헨",
    "Muenchen": "뮌헨",
    "Vienna": "빈",
    "Wien": "빈",
    "Interlaken": "인터라켄",
    "Venice": "베네치아",
    "Venezia": "베네치아",
    "Florence": "피렌체",
    "Firenze": "피렌체",
    "Rome": "로마",
    "Roma": "로마",
    "Madrid": "마드리드",
    "Barcelona": "바르셀로나",
    "Prague": "프라하",
    "Praha": "프라하",
    "Sydney": "시드니",
    "Melbourne": "멜버른",
    "Perth": "퍼스",

    # Korean aliases
    "제주도": "제주",
}

CITY_HERO_FILES = {
    "서울": "Seoul Gyeongbokgung Blue House Bukhansan cropped.jpg",
    "부산": "Gwangan Bridge at Night, Busan.jpg",
    "제주": "Seongsan Ilchulbong cliff facing the sea.jpg",
    "도쿄": "Tokyo Tower and around Buildings.jpg",
    "오사카": "Dotonbori, Osaka, at night, November 2016.jpg",
    "방콕": "Templo Wat Arun, Bangkok, Tailandia, 2013-08-22, DD 37.jpg",
    "호치민": "Notre-Dame Cathedral Basilica of Saigon.jpg",
    "다낭": "My Khe Beach Danang Coastline.jpg",
    "런던": "London, Tower Bridge -- 2016 -- 4772.jpg",
    "파리": "Eiffel Tower at night.JPG",
    "암스테르담": "Amsterdam Canal at Night.JPG",
    "베를린": "Brandenburg Gate at night.jpg",
    "뮌헨": "Marienplatz, Munich, Germany.jpg",
    "비엔나": "Schönbrunn Palace - Vienna.jpg",
    "인터라켄": "Interlaken, Switzerland - Panorama (cropped).jpg",
    "베니스": "Venice view, Grand Canal, Venice, Italy.jpg",
    "피렌체": "Florence Cathedral Duomo from Piazzale Michelangelo.jpg",
    "로마": "Colosseum in Rome-April 2007-1-_copie_2B.jpg",
    "마드리드": "Plaza Mayor, Madrid, Spain - Oct 2009.jpg",
    "바르셀로나": "Sagrada Familia - Barcelona - Spain.jpg",
    "프라하": "Charles Bridge Prague.jpg",
    "시드니": "The Sydney Opera House at dusk.jpg",
    "멜버른": "Flinders st station melb.jpg",
    "퍼스": "Perth skyline from Kings Park May 2018.jpg",
}


def _commons_filepath(file_name: str, width: int = 2000) -> str:
    """Build a Wikimedia Commons Special:FilePath URL safely."""
    file_name = (file_name or "").strip()
    if not file_name:
        return ""
    base = f"https://commons.wikimedia.org/wiki/Special:FilePath/{quote(file_name)}"
    if isinstance(width, int) and width > 0:
        return f"{base}?width={width}"
    return base



COUNTRY_HERO_OVERRIDES = {
    '대한민국': 'https://commons.wikimedia.org/wiki/Special:FilePath/Seoul%20Skyline%20%28Han%20River%29.jpg?width=1800',
    '일본': 'https://commons.wikimedia.org/wiki/Special:FilePath/Mount%20Fuji%20from%20Lake%20Kawaguchi.jpg?width=1800',
}


# ---------------------------------------------------------
# ✅ Local fixed city hero mapping (for stable preview & PDF)
#   - Files live at: static/img/city_hero/<slug>
#   - Download once via: python scripts/download_city_hero_images.py
# ---------------------------------------------------------
CITY_HERO_SLUGS = {
    ("대한민국","서울"): "kr_seoul.jpg",
    ("대한민국","부산"): "kr_busan.jpg",
    ("대한민국","제주"): "kr_jeju.jpg",

    ("일본","도쿄"): "jp_tokyo.jpg",
    ("일본","오사카"): "jp_osaka.jpg",

    ("태국","방콕"): "th_bangkok.jpg",

    ("베트남","호치민"): "vn_hcmc.jpg",
    ("베트남","다낭"): "vn_danang.jpg",

    ("영국","런던"): "uk_london.jpg",

    ("프랑스","파리"): "fr_paris.jpg",

    ("네덜란드","암스테르담"): "nl_amsterdam.jpg",

    ("독일","베를린"): "de_berlin.jpg",
    ("독일","뮌헨"): "de_munich.jpg",

    ("오스트리아","빈"): "at_vienna.jpg",

    ("스위스","인터라켄"): "ch_interlaken.jpg",

    ("이탈리아","베네치아"): "it_venice.jpg",
    ("이탈리아","피렌체"): "it_florence.jpg",
    ("이탈리아","로마"): "it_rome.jpg",

    ("스페인","마드리드"): "es_madrid.jpg",
    ("스페인","바르셀로나"): "es_barcelona.jpg",

    ("체코","프라하"): "cz_prague.jpg",

    ("호주","시드니"): "au_sydney.jpg",
    ("호주","멜버른"): "au_melbourne.jpg",
    ("호주","퍼스"): "au_perth.jpg",
}
# (country, city) -> landmark search query (English keywords for better hit rate)
# Used to auto-fetch deterministic city hero photos (landmark-focused) and cache locally.
CITY_LANDMARK_QUERY: Dict[Tuple[str, str], str] = {
    ("대한민국","서울"): "Gyeongbokgung Palace",
    ("대한민국","부산"): "Haeundae Beach",
    ("대한민국","제주"): "Seongsan Ilchulbong",

    ("일본","도쿄"): "Tokyo Tower",
    ("일본","오사카"): "Osaka Castle",

    ("태국","방콕"): "Wat Arun Bangkok",

    ("베트남","호치민"): "Notre-Dame Cathedral Basilica of Saigon",
    ("베트남","다낭"): "Golden Bridge Da Nang",

    ("영국","런던"): "Tower Bridge London",
    ("프랑스","파리"): "Eiffel Tower",
    ("네덜란드","암스테르담"): "Canals of Amsterdam",
    ("독일","베를린"): "Brandenburg Gate",
    ("독일","뮌헨"): "Marienplatz Munich",

    ("오스트리아","빈"): "Schonbrunn Palace",
    ("스위스","인터라켄"): "Interlaken Switzerland Alps",

    ("이탈리아","베네치아"): "Grand Canal Venice",
    ("이탈리아","피렌체"): "Florence Cathedral",
    ("이탈리아","로마"): "Colosseum",

    ("스페인","마드리드"): "Plaza Mayor Madrid",
    ("스페인","바르셀로나"): "Sagrada Familia",

    ("체코","프라하"): "Charles Bridge Prague",

    ("호주","시드니"): "Sydney Opera House",
    ("호주","멜버른"): "Flinders Street railway station",
    ("호주","퍼스"): "Kings Park and Botanic Garden Perth",
}

# If the packaged placeholder is present (small size), we try to replace it with a real landmark photo.
_CITY_HERO_MIN_REAL_BYTES = 120_000  # placeholders(~50-75KB) are ignored; real photos are usually >120KB_000

def _city_hero_project_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

def _city_hero_local_path(slug: str) -> str:
    return os.path.join(_city_hero_project_root(), "static", "img", "city_hero", slug)

def _city_hero_credits_path() -> str:
    return os.path.join(_city_hero_project_root(), "static", "img", "city_hero", "credits.json")

def _read_city_hero_credits() -> Dict[str, Any]:
    try:
        p = _city_hero_credits_path()
        if not os.path.exists(p):
            return {}
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}

def _write_city_hero_credits(data: Dict[str, Any]) -> None:
    try:
        p = _city_hero_credits_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def _save_city_hero(slug: str, img_bytes: bytes) -> bool:
    """Save bytes as a JPEG to the local hero slot (slug)."""
    try:
        from io import BytesIO
        from PIL import Image
        local_path = _city_hero_local_path(slug)
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        im = Image.open(BytesIO(img_bytes))
        if im.mode not in ("RGB",):
            im = im.convert("RGB")
        im.save(local_path, format="JPEG", quality=88, optimize=True, progressive=True)
        return os.path.exists(local_path) and os.path.getsize(local_path) > 10_000
    except Exception:
        return False

def _ensure_city_hero_downloaded(country: str, city: str, slug: str, width: int = 1600) -> Optional[str]:
    """Try to download a landmark-focused hero image (Wikipedia) and cache it locally.

    Returns:
      - '/static/img/city_hero/<slug>' if cached successfully
      - otherwise a remote thumbnail URL (still landmark-focused) if available
      - otherwise None
    """
    key = f"cityhero_dl:{country}:{city}:{slug}:{width}"
    if _CACHE.get(key) == "fail":
        return None

    q = CITY_LANDMARK_QUERY.get((country, city)) or CITY_LANDMARK_QUERY.get(("", city))
    if not q:
        return None

    meta = _wikimedia_thumb_meta(q, thumb_size=min(max(int(width), 600), 2000), timeout=8)
    if not meta:
        _CACHE[key] = "fail"
        return None

    thumb_url = meta.get("thumb_url")
    if not thumb_url:
        _CACHE[key] = "fail"
        return None

    # Local target path (used for cache-busting and credit bookkeeping)
    local_path = _city_hero_local_path(slug)

    # Attempt to download and cache locally (overwrite placeholder if any).
    try:
        r = requests.get(thumb_url, timeout=12, headers={"User-Agent": "TravelAI/CityHeroAutoCache"})
        if r.status_code == 200 and r.content and len(r.content) > 10_000:
            ok = _save_city_hero(slug, r.content)
            if ok:
                credits = _read_city_hero_credits()
                credits[slug] = {
                    "query": q,
                    "thumb_url": thumb_url,
                    "page_url": meta.get("page_url"),
                    "fetched_at": datetime.utcnow().isoformat() + "Z",
                }
                _write_city_hero_credits(credits)
                return f"/static/img/city_hero/{slug}?v={int(os.path.getmtime(local_path))}"
    except Exception:
        pass

    # If caching failed, at least return a deterministic landmark thumbnail.
    return thumb_url

# =========================================================
# ✅ City hero pack downloader (no script needed)
#    - Used by UI tool button to download & cache fixed city hero images into static/img/city_hero
# =========================================================

_CITY_HERO_PINNED_ALIASES = {
    "빈": "비엔나",
    "비엔나": "비엔나",
    "베네치아": "베니스",
    "베니스": "베니스",
}

def download_city_hero_pack(force: bool = False, width: int = 1600) -> Dict[str, Any]:
    """Download (or refresh) all city hero images into the project's static folder.

    This is designed for the UI button (no separate script needed).

    Args:
      force: If True, re-download even if a local file exists.
      width: target width for Wikimedia thumbnails (kept wide ~16:9).

    Returns:
      Summary dict for UI rendering.
    """
    results: List[Dict[str, Any]] = []
    ok = 0
    dl = 0
    errors = 0

    items = list(CITY_HERO_SLUGS.items())

    for (country, city), slug in items:
        city_norm = _norm_city_key(city)
        local_path = _city_hero_local_path(slug)
        prev_size = os.path.getsize(local_path) if os.path.exists(local_path) else 0

        # If already a real file and not forcing, skip.
        if (not force) and os.path.exists(local_path):
            try:
                if os.path.getsize(local_path) >= _CITY_HERO_MIN_REAL_BYTES:
                    results.append({"country": country, "city": city, "slug": slug, "status": "ok", "size": os.path.getsize(local_path)})
                    ok += 1
                    continue
            except Exception:
                pass

        pinned_city = city_norm
        if pinned_city not in CITY_HERO_FILES:
            pinned_city = _CITY_HERO_PINNED_ALIASES.get(pinned_city, pinned_city)

        pinned_file = CITY_HERO_FILES.get(pinned_city)
        used_page = None

        # 1) pinned Wikimedia file (most deterministic)
        pinned_ok = False
        if pinned_file:
            try:
                remote_url = _commons_filepath(pinned_file, width=width)
                r = requests.get(remote_url, timeout=15, headers={"User-Agent": "TravelAI/CityHeroPack"})
                if r.status_code == 200 and r.content and len(r.content) > 10_000:
                    pinned_ok = _save_city_hero(slug, r.content)
                    if pinned_ok:
                        used_page = "https://commons.wikimedia.org/wiki/File:" + pinned_file.replace(" ", "_")
            except Exception:
                pinned_ok = False

        # 2) query-based landmark (fallback)
        if not pinned_ok:
            try:
                _ensure_city_hero_downloaded(country, city_norm, slug, width=width)
            except Exception:
                pass

        # status check
        try:
            new_size = os.path.getsize(local_path) if os.path.exists(local_path) else 0
            if new_size >= _CITY_HERO_MIN_REAL_BYTES:
                ok += 1
                if prev_size < _CITY_HERO_MIN_REAL_BYTES:
                    dl += 1

                # credit bookkeeping (best-effort)
                if pinned_ok and pinned_file:
                    try:
                        credits = _read_city_hero_credits()
                        credits[slug] = {
                            "source": "wikimedia_commons",
                            "file": pinned_file,
                            "file_page": used_page,
                            "fetched_at": datetime.utcnow().isoformat() + "Z",
                        }
                        _write_city_hero_credits(credits)
                    except Exception:
                        pass

                results.append({"country": country, "city": city, "slug": slug, "status": "ok", "size": new_size})
            else:
                errors += 1
                results.append({"country": country, "city": city, "slug": slug, "status": "fail", "size": new_size})
        except Exception:
            errors += 1
            results.append({"country": country, "city": city, "slug": slug, "status": "error", "size": 0})

    skipped = max(0, ok - dl)

    return {
        "total": len(results),
        "ok": ok,
        "downloaded": dl,
        "skipped": skipped,
        "errors": errors,
        "results": results,
    }

def _norm_city_key(city: str) -> str:
    c = (city or '').strip()
    return CITY_NAME_ALIASES.get(c, c)


_PLACES_DB_CACHE: Optional[PlacesCache] = None


def _places_cache() -> PlacesCache:
    global _PLACES_DB_CACHE
    if _PLACES_DB_CACHE is None:
        _PLACES_DB_CACHE = PlacesCache()
    return _PLACES_DB_CACHE


def _ttl_seconds(env_key: str, default_days: int) -> int:
    try:
        days = int((os.getenv(env_key) or str(default_days)).strip())
    except Exception:
        days = default_days
    days = max(1, min(30, days))
    return int(days * 24 * 60 * 60)

def _get_image_provider() -> str:
    """Image provider selection.

    Values:
      - auto (default): if GOOGLE_PLACES_API_KEY exists -> google_places, else unsplash
      - unsplash: always use Unsplash (fallback picsum)
      - google_places: try Google Places photo first, then fallback to Unsplash
    """
    v = (os.getenv("IMAGE_PROVIDER") or "auto").strip().lower()
    if v in ("auto", "unsplash", "google_places", "wikimedia", "wiki"):
        return v
    return "auto"

def _get_google_places_key() -> str:
    # Support both historical env var names
    return (
        os.getenv("GOOGLE_MAPS_API_KEY")
        or os.getenv("GOOGLE_PLACES_API_KEY")
        or os.getenv("Google_Places_API_Key")
        or ""
    ).strip()


# --- Google Places name normalization helpers ---
_LATIN_RE = re.compile(r"[A-Za-z]")


def _has_latin(s: str) -> bool:
    return bool(_LATIN_RE.search(s or ""))


def _google_places_details_name(place_id: str, timeout: int = 6) -> str:
    """Fetch English place name via Place Details (legacy).

    Called only when Text Search returns a non-Latin name (e.g. Japanese/Chinese script)
    even with language=en. This is best-effort and cached by the caller.
    """
    pid = (place_id or "").strip()
    key = _get_google_places_key()
    if not pid or not key:
        return ""
    cache_key = f"gplaces_details_name:{pid}"
    if cache_key in _CACHE:
        return _CACHE[cache_key] or ""
    try:
        url = "https://maps.googleapis.com/maps/api/place/details/json"
        params = {
            "place_id": pid,
            "fields": "name",
            "language": "en",
            "key": key,
        }
        r = requests.get(url, params=params, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            _CACHE[cache_key] = ""
            return ""
        data = r.json() or {}
        nm = (((data.get("result") or {}) or {}).get("name") or "").strip()
        _CACHE[cache_key] = nm
        return nm
    except Exception:
        _CACHE[cache_key] = ""
        return ""


def google_places_details_name(place_id: str) -> str:
    """Public wrapper for getting an English-ish place name from a place_id."""
    return _google_places_details_name(place_id)

def _google_places_text_search(query: str, lat: float = None, lon: float = None, radius_m: int = 12000, timeout: int = 6) -> Optional[Dict[str, Any]]:
    """Places Text Search (legacy) to fetch a photo_reference.

    NOTE: Uses legacy endpoint for simplicity. Google also offers the newer Places API endpoints.
    """
    key = _get_google_places_key()
    if not key or not query:
        return None

    # --- Persistent TTL cache (SQLite) ---
    # Cache key explosion can be large, so we keep DB keys hashed in the cache layer.
    try:
        qttl = _ttl_seconds("PLACE_QUERY_TTL_DAYS", 14)
        pttl = _ttl_seconds("PLACE_DETAILS_TTL_DAYS", 14)
        db = _places_cache()
        pid = db.get_place_id_for_query(query, lat=lat, lon=lon, radius_m=radius_m)
        if pid:
            cached = db.get_place(pid)
            if cached and (cached.get("name") or cached.get("photo_reference")):
                out = {
                    "name": cached.get("name") or "",
                    "place_id": cached.get("place_id") or pid,
                    "photo_reference": cached.get("photo_reference") or "",
                    "rating": cached.get("rating"),
                    "user_ratings_total": cached.get("user_ratings_total"),
                }
                _CACHE[f"gplaces_pid:{pid}"] = out
                _CACHE[f"gplaces_text:{query}:{lat}:{lon}:{radius_m}"] = out
                return out
    except Exception:
        # Never break image/name flow due to cache errors
        pass
    cache_key = f"gplaces_text:{query}:{lat}:{lon}:{radius_m}"
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
    params: Dict[str, Any] = {
        "query": query,
        "key": key,
        # Force English results when possible
        "language": "en",
    }
    if lat is not None and lon is not None:
        params["location"] = f"{lat},{lon}"
        params["radius"] = int(radius_m)

    try:
        r = requests.get(url, params=params, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return None
        data = r.json() or {}
        results = data.get("results") or []
        if not results:
            return None
        best = results[0] or {}
        photos = best.get("photos") or []
        photo_ref = None
        if photos:
            photo_ref = (photos[0] or {}).get("photo_reference")
        out = {
            "name": best.get("name") or "",
            "place_id": best.get("place_id") or "",
            "photo_reference": photo_ref or "",
            "rating": best.get("rating"),
            "user_ratings_total": best.get("user_ratings_total"),
            "price_level": best.get("price_level"),
        }

        # If Google returns a non-Latin name even with language=en,
        # try Place Details for a better English display name (best-effort).
        try:
            pid2 = (out.get("place_id") or "").strip()
            nm2 = (out.get("name") or "").strip()
            if pid2 and nm2 and (not _has_latin(nm2)):
                dn = (_google_places_details_name(pid2) or "").strip()
                if dn:
                    out["name"] = dn
        except Exception:
            pass

        # --- Save to DB cache ---
        try:
            pid = (out.get("place_id") or "").strip()
            if pid:
                db = _places_cache()
                qttl = _ttl_seconds("PLACE_QUERY_TTL_DAYS", 14)
                pttl = _ttl_seconds("PLACE_DETAILS_TTL_DAYS", 14)
                db.set_query_mapping(query, pid, ttl_seconds=qttl, lat=lat, lon=lon, radius_m=radius_m)
                db.set_place(pid, name_en=out.get("name") or "", photo_reference=out.get("photo_reference") or "", lat=lat, lon=lon, rating=out.get("rating"), user_ratings_total=out.get("user_ratings_total"), ttl_seconds=pttl)
        except Exception:
            pass

        _CACHE[cache_key] = out
        return out
    except Exception:
        return None


def google_places_resolve(query: str, lat: float = None, lon: float = None, radius_m: int = 12000) -> Optional[Dict[str, Any]]:
    """Resolve a place using Google Places Text Search (legacy).

    Returns a dict with keys:
      - name: Google place name (language=en)
      - place_id: stable id for dedupe
      - photo_reference: for Places Photo API

    This is intentionally lightweight and cached via the internal _CACHE.
    """
    return _google_places_text_search(query=query, lat=lat, lon=lon, radius_m=radius_m)

def google_places_photo_proxy_url(photo_reference: str, w: int = 900) -> Optional[str]:
    """Return internal proxy URL (do not expose API key to browser)."""
    if not photo_reference:
        return None
    # Django API endpoint
    return f"/api/place_photo/?ref={quote(str(photo_reference))}&w={int(w)}"

# --- Wikimedia / Wikipedia thumbnail (optional) ---

_WIKI_API = "https://en.wikipedia.org/w/api.php"


def _wikimedia_thumb(search_query: str, thumb_size: int = 900, timeout: int = 6) -> Optional[str]:
    """Fetch a relevant thumbnail from English Wikipedia using MediaWiki API.

    Returns a direct image URL (thumbnail.source) if found, else None.
    Lightweight + cached to keep UX snappy.
    """
    q = (search_query or "").strip()
    if not q:
        return None
    cache_key = f"wiki:{thumb_size}:{q.lower()}"
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    params = {
        "action": "query",
        "format": "json",
        "prop": "pageimages",
        "pithumbsize": int(thumb_size),
        "generator": "search",
        "gsrsearch": q,
        "gsrlimit": 1,
        "gsrwhat": "text",
        "origin": "*",
        "redirects": 1,
    }
    try:
        r = requests.get(_WIKI_API, params=params, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            _CACHE[cache_key] = None
            return None
        data = r.json() or {}
        pages = ((data.get("query") or {}).get("pages") or {})
        if not pages:
            _CACHE[cache_key] = None
            return None
        # pick first page
        first = next(iter(pages.values()))
        thumb = (first or {}).get("thumbnail") or {}
        src = thumb.get("source")
        _CACHE[cache_key] = src
        return src
    except Exception:
        _CACHE[cache_key] = None
        return None


def _wikimedia_thumb_meta(search_query: str, thumb_size: int = 900, timeout: int = 6) -> Optional[Dict[str, Any]]:
    """Wikipedia thumbnail + page link meta.

    Returns a dict like:
      {"thumb_url": str, "page_url": str, "title": str, "pageid": int}
    """
    q = (search_query or "").strip()
    if not q:
        return None
    cache_key = f"wiki_meta:{thumb_size}:{q.lower()}"
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    params = {
        "action": "query",
        "format": "json",
        "prop": "pageimages",
        "pithumbsize": int(thumb_size),
        "generator": "search",
        "gsrsearch": q,
        "gsrlimit": 1,
        "gsrwhat": "text",
        "origin": "*",
        "redirects": 1,
    }
    try:
        r = requests.get(_WIKI_API, params=params, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            _CACHE[cache_key] = None
            return None
        data = r.json() or {}
        pages = ((data.get("query") or {}).get("pages") or {})
        if not pages:
            _CACHE[cache_key] = None
            return None
        first = next(iter(pages.values())) or {}
        thumb = (first.get("thumbnail") or {})
        src = thumb.get("source")
        pageid = first.get("pageid")
        title = (first.get("title") or "").strip()
        page_url = f"https://en.wikipedia.org/?curid={int(pageid)}" if pageid else ""
        out = {
            "thumb_url": src,
            "page_url": page_url,
            "title": title,
            "pageid": int(pageid) if pageid else None,
        }
        _CACHE[cache_key] = out
        return out
    except Exception:
        _CACHE[cache_key] = None
        return None

# City romanization hints for better Unsplash relevance
_CITY_QUERY = {
    "서울": "seoul",
    "부산": "busan",
    "도쿄": "tokyo",
    "오사카": "osaka",
    "교토": "kyoto",
    "삿포로": "sapporo",
    "후쿠오카": "fukuoka",
    "파리": "paris",
    "런던": "london",
    "뉴욕": "new york",
    "로스앤젤레스": "los angeles",
    "LA": "los angeles",
    "샌프란시스코": "san francisco",
    "밴쿠버": "vancouver",
    "토론토": "toronto",
    "방콕": "bangkok",
    "하노이": "hanoi",
    "호치민": "ho chi minh city",
    "다낭": "da nang",
    "로마": "rome",
    "밀라노": "milan",
    "바르셀로나": "barcelona",
    "마드리드": "madrid",
    "베를린": "berlin",
    "암스테르담": "amsterdam",
    "취리히": "zurich",
    "빈": "vienna"
}

def _city_query(city_name: str) -> str:
    c = (city_name or "").strip()
    if not c:
        return ""
    return _CITY_QUERY.get(c, c)

def _safe_place_query(place_name: str) -> str:
    """호텔/장소명 검색용 쿼리 정리
    - 비ASCII(일본어 등)도 허용 (호텔 실명 기반 검색 품질 개선)
    - 너무 길거나 특수문자 위주면 축약
    """
    if not place_name:
        return ""
    s = str(place_name).strip()
    if not s:
        return ""
    # 너무 길면 축약
    if len(s) > 48:
        s = s[:48]
    # 특수문자 정리
    s = re.sub(r"[\r\n\t]+", " ", s)
    s = re.sub(r"\s{2,}", " ", s).strip()
    return s



def _get_unsplash_key() -> str:
    # NOTE: read env at runtime (load_dotenv may run after import)
    return os.getenv("Unsplash_Access_Key", "").strip()

def _stable_int(seed: str) -> int:
    s = (seed or "seed").encode("utf-8", errors="ignore")
    return int(hashlib.md5(s).hexdigest(), 16)

def _picsum(seed: str, w: int, h: int) -> str:
    safe = "".join([c if c.isalnum() or c in "_-" else "_" for c in (seed or "img")])[:60]
    return f"https://picsum.photos/seed/{safe}/{w}/{h}"

def _build_unsplash_sized_url(raw_or_regular: str, w: int, h: int) -> str:
    # Unsplash raw URL supports sizing params; regular/full often already sized.
    if not raw_or_regular:
        return ""
    sep = "&" if "?" in raw_or_regular else "?"
    return f"{raw_or_regular}{sep}w={w}&h={h}&fit=crop&auto=format"

def _unsplash_search(query: str, per_page: int = 30, timeout: int = 10) -> List[Dict[str, Any]]:
    key = _get_unsplash_key()
    if not key:
        return []
    cache_key = f"unsplash_search:{query}:{per_page}"
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    url = "https://api.unsplash.com/search/photos"
    # content_filter=high helps reduce unrelated/low quality results
    params = {
        "query": query,
        "per_page": per_page,
        "orientation": "landscape",
        "content_filter": "high",
        "order_by": "relevant",
    }
    headers = {"Authorization": f"Client-ID {key}"}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=timeout)
        if r.status_code != 200:
            return []
        data = r.json() or {}
        results = data.get("results") or []
        _CACHE[cache_key] = results
        return results
    except Exception:
        return []

def unsplash_pick_image(query: str, seed: str, w: int = 900, h: int = 600) -> Optional[str]:
    """Unsplash 검색 결과 중 seed 기반으로 하나를 선택하여 size URL 반환"""
    # Pull more candidates so different hotels can pick different images
    results = _unsplash_search(query, per_page=36)
    if not results:
        return None

    idx = _stable_int(seed) % len(results)
    item = results[idx] or {}
    urls = item.get("urls") or {}
    raw = urls.get("raw") or urls.get("full") or urls.get("regular") or ""
    if not raw:
        return None
    return _build_unsplash_sized_url(raw, w=w, h=h)


def unsplash_pick_meta(query: str, seed: str, w: int = 900, h: int = 600) -> Optional[Dict[str, Any]]:
    """Unsplash search -> pick one item (seed-stable) and return image + credit meta.

    Returns:
      {
        "url": str,
        "photo_page": str,
        "user_name": str,
        "user_page": str,
      }
    """
    results = _unsplash_search(query, per_page=36)
    if not results:
        return None
    idx = _stable_int(seed) % len(results)
    item = results[idx] or {}
    urls = item.get("urls") or {}
    raw = urls.get("raw") or urls.get("full") or urls.get("regular") or ""
    if not raw:
        return None
    link = (item.get("links") or {}).get("html") or ""
    user = item.get("user") or {}
    user_name = (user.get("name") or user.get("username") or "").strip()
    user_page = (user.get("links") or {}).get("html") or ""
    return {
        "url": _build_unsplash_sized_url(raw, w=w, h=h),
        "photo_page": link,
        "user_name": user_name,
        "user_page": user_page,
    }

def get_poi_images(
    place_name: str,
    city_name: str,
    typ: str,
    seed: str,
    place_id: str = "",
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    radius_m: int = 12000,
    **_kwargs,
) -> Tuple[str, str]:
    """Return (web_image_url, hd_image_url) for a POI.

    Notes
    - The Django port sometimes passes extra context (place_id/lat/lon). The earlier
      Flask version accepted these and used them for better image candidates.
    - In v1 we keep a lightweight strategy:
        1) (optional) Google Places textsearch near (lat,lon) → photo
        2) Unsplash (if key exists)
        3) Deterministic placeholder (picsum) so UI never breaks.
    """

    place_name = (place_name or "").strip()
    city_name = (city_name or "").strip()
    typ = (typ or "Attraction").strip()
    seed = (seed or f"poi_{place_name}_{city_name}").strip()

    # 0) If place_name is empty, return fallback immediately
    if not place_name:
        return _picsum(seed, 900, 600), _picsum(seed + "_hd", 1600, 900)

    provider = _get_image_provider()

    # 1) Try Google Places (textsearch -> photo_reference). Requires GOOGLE_MAPS_API_KEY.
    g_url = None
    if provider in ("auto", "google", "google_places"):
        try:
            q = f"{place_name} {city_name}".strip()
            g = _google_places_text_search(q, lat=lat, lon=lon, radius_m=radius_m)
            if g and g.get("photo_url"):
                g_url = g.get("photo_url")
        except Exception:
            g_url = None

    # 2) Try Unsplash
    u_url = None
    if provider in ("auto", "unsplash", "google_places"):
        try:
            q = f"{place_name} {city_name} {typ}".strip()
            u_url = unsplash_pick_image(q, seed=seed, w=900, h=600)
        except Exception:
            u_url = None

    # 3) Final fallback (always)
    web = g_url or u_url or _picsum(seed, 900, 600)
    hd = g_url or u_url or _picsum(seed + "_hd", 1600, 900)
    return web, hd

def get_hotel_images(city_name: str, seed: str, hotel_name: str = "", country_name: str = "") -> Tuple[str, str]:
    """숙소: 웹용, HD용 URL 반환

    ✅ 개선:
    - 모든 호텔이 같은 이미지가 나오지 않도록 seed를 호텔별로 주도록 설계
    - hotel_name(가능할 때) + 도시 기반으로 검색 쿼리를 더 정확히 구성
    - 일본어/한자 등 비ASCII 호텔명은 자동으로 제외(검색 품질 방어)
    """
    city_q = _city_query(city_name)
    hotel_q = _safe_place_query(hotel_name)

    queries: List[str] = []
    if hotel_q:
        queries.append(f"{hotel_q} {city_q} hotel exterior")
        queries.append(f"{hotel_q} hotel exterior")
    if country_name:
        queries.append(f"{city_q} {country_name} hotel exterior")
    queries.extend([
        f"{city_q} hotel exterior",
        f"{city_q} hotel building",
        f"{city_q} hotel lobby",
        f"{city_q} hotel room",
    ])

    provider = _get_image_provider()
    web = None
    hd = None

    # 1) Google Places (optional) - hotel photo tends to be more accurate
    if provider in ("auto", "google_places") and _get_google_places_key():
        base_q = " ".join([x for x in [hotel_q, city_q, country_name, "hotel"] if x]).strip()
        if base_q:
            g = _google_places_text_search(base_q)
            pref = (g or {}).get("photo_reference")
            if pref:
                web = google_places_photo_proxy_url(pref, w=900)
                hd  = google_places_photo_proxy_url(pref, w=1600)
    for q in queries:
        q = (q or "").strip()
        if not q:
            continue
        web = web or unsplash_pick_image(q, seed=seed, w=900, h=600)
        hd  = hd  or unsplash_pick_image(q, seed=seed + "_hd", w=1920, h=1200)
        if web and hd:
            break

    if not web:
        # 마지막 fallback: 태그 기반 featured 이미지 (대체로 관련도가 좋음)
        tags = ",".join([t for t in [city_q, "hotel", "exterior"] if t])
        sig = _stable_int(seed) % 10000
        web = f"https://source.unsplash.com/900x600/?{tags}&sig={sig}"
    if not hd:
        tags = ",".join([t for t in [city_q, "hotel", "exterior"] if t])
        sig = _stable_int(seed + "_hd") % 10000
        hd = f"https://source.unsplash.com/1920x1200/?{tags}&sig={sig}"
    return web, hd


def get_image_candidates(
    kind: str,
    place_name: str,
    city_name: str = "",
    country_name: str = "",
    place_id: str = "",
    poi_type: str = "",
    seed: str = "",
    lat: float = None,
    lon: float = None,
    w: int = 1200,
    h: int = 800,
) -> List[Dict[str, Any]]:
    """Return a small list of image candidates from multiple providers.

    The list is ordered as:
      1) Google Places (if available)
      2) Wikipedia (best effort)
      3) Unsplash (fallback)

    Each item format:
      {
        "source": "google"|"wiki"|"unsplash",
        "url": str,
        "credit": {"text": str, "href": optional str}
      }

    NOTE: This function only returns URLs/metadata. The caller decides whether
    to lazy-load it (recommended) to control cost.
    """
    kind = (kind or "").strip().lower()
    poi_type = (poi_type or "").strip()
    seed = (seed or "").strip() or (place_id or place_name or "seed")

    # Query building: keep it short but specific.
    base = " ".join([x for x in [place_name, city_name, country_name] if (x or "").strip()]).strip()
    q = base
    if kind in ("hotel", "stay", "accommodation"):
        q = f"{base} hotel".strip()
    elif kind in ("poi", "place"):
        if poi_type == "Restaurant":
            q = f"{base} restaurant".strip()
        elif poi_type == "Cafe":
            q = f"{base} cafe".strip()
        elif poi_type == "Attraction":
            q = f"{base} landmark".strip()

    images: List[Dict[str, Any]] = []

    # 1) Google Places photo (best precision)
    if _get_google_places_key():
        try:
            pref = ""
            # Try cached place details first when place_id exists
            if place_id:
                cached = _places_cache().get_place(place_id)
                pref = (cached or {}).get("photo_reference") or ""
            if not pref and q:
                g = google_places_resolve(q, lat=lat, lon=lon)
                pref = (g or {}).get("photo_reference") or ""
            if pref:
                url = google_places_photo_proxy_url(pref, w=max(600, int(w)))
                if url:
                    images.append({
                        "source": "google",
                        "url": url,
                        "credit": {"text": "Google Places"},
                    })
        except Exception:
            pass

    # 2) Wikipedia thumbnail (use cleaner query: place + city)
    try:
        wiki_q = " ".join([x for x in [place_name, city_name] if (x or "").strip()]).strip() or q
        wm = _wikimedia_thumb_meta(wiki_q, thumb_size=max(600, int(w)))
        if wm and wm.get("thumb_url"):
            images.append({
                "source": "wiki",
                "url": wm.get("thumb_url"),
                "credit": {"text": "Wikipedia", "href": wm.get("page_url") or ""},
            })
    except Exception:
        pass

    # 3) Unsplash (fallback)
    try:
        u = unsplash_pick_meta(q or base, seed=seed, w=max(600, int(w)), h=max(400, int(h)))
        if u and u.get("url"):
            who = (u.get("user_name") or "").strip()
            credit_text = f"Unsplash · {who}" if who else "Unsplash"
            images.append({
                "source": "unsplash",
                "url": u.get("url"),
                "credit": {"text": credit_text, "href": u.get("photo_page") or u.get("user_page") or ""},
            })
    except Exception:
        pass

    # If absolutely nothing, fallback to picsum
    if not images:
        images.append({
            "source": "fallback",
            "url": _picsum(seed, max(600, int(w)), max(400, int(h))),
            "credit": {"text": "placeholder"},
        })

    return images



# -----------------------------
# Backward compatible wrappers
# (old names used by previous versions)
# -----------------------------

def unsplash_first_image(query: str) -> Optional[str]:
    """Return a single web-sized image URL for a query."""
    return unsplash_pick_image(query, seed=query, w=900, h=600)

def get_city_image(city_name: str, country_name: str = "", width: int = 1600) -> str:
    """City/region image for web cards.

    Priority:
    1) Local fixed city hero (static/img/city_hero) if exists
    2) Wikimedia Commons deterministic hero (via CITY_HERO_FILES)
    3) Unsplash (fallback)
    4) Picsum (last resort)

    NOTE:
    - width is accepted for backward-compatibility (some call sites pass width=...).
    - To fully fix preview stability, run `python scripts/download_city_hero_images.py`
      once to download all city heroes into the static folder.
    """
    city = (city_name or '').strip()
    country = (country_name or '').strip()
    city = _norm_city_key(city)

    # -------------------------------------------------
    # 1) Local static city heroes (fixed & deterministic)
    # -------------------------------------------------
    try:
        slug = CITY_HERO_SLUGS.get((country, city)) or CITY_HERO_SLUGS.get(("", city))
        if slug:
            project_root = _city_hero_project_root()
            local_path = _city_hero_local_path(slug)
            # If we have a shipped/cached hero image on disk, use it.
            if os.path.exists(local_path):
                sz = os.path.getsize(local_path)
                if sz >= _CITY_HERO_MIN_REAL_BYTES:
                    return f"/static/img/city_hero/{slug}?v={int(os.path.getmtime(local_path))}"

            # Otherwise, try to auto-download a landmark photo once and cache it.
            auto = _ensure_city_hero_downloaded(country, city, slug, width=width)
            if auto:
                return auto
    except Exception:
        pass

# 2) Wikimedia deterministic overrides
    # -------------------------------------------------
    try:
        if city in CITY_HERO_FILES:
            url = _commons_filepath(CITY_HERO_FILES.get(city, ''), width=width)
            if isinstance(width, int) and width > 0 and url.startswith("https://commons.wikimedia.org/wiki/Special:FilePath/") and "width=" not in url:
                url = url + (("?width=%d" % width) if ("?" not in url) else ("&width=%d" % width))
            return url
        if country in COUNTRY_HERO_OVERRIDES:
            url = COUNTRY_HERO_OVERRIDES[country]
            if isinstance(width, int) and width > 0 and url.startswith("https://commons.wikimedia.org/wiki/Special:FilePath/") and "width=" not in url:
                url = url + (("?width=%d" % width) if ("?" not in url) else ("&width=%d" % width))
            return url
    except Exception:
        pass

    # -------------------------------------------------
    # 3) Fallback provider
    # -------------------------------------------------
    prov = _get_image_provider()
    if prov in ("wikimedia", "wiki"):
        try:
            cands = get_image_candidates(kind="city", place_name=city, city_name=city, country_name=country, w=width, h=int(width*0.56))
            return cands[0]["url"] if cands else ""
        except Exception:
            prov = "unsplash"

    if prov in ("auto", "unsplash", "google_places"):
        q = f"{city} {country} skyline landmark".strip()
        return _unsplash_url(q, w=width, h=int(width*0.56))

    return _picsum_url(width, int(width*0.56))

def get_place_image(
    place_name: str,
    city_name: str = "",
    country_name: str = "",
    place_id: str = "",
    poi_type: str = "",
    lat: float = None,
    lon: float = None,
    w: int = 1100,
    h: int = 640,
) -> str:
    """Backward-compatible wrapper: return a single image URL for a POI/place.

    Prefers Google Places photo (if available) -> Wikipedia -> Unsplash -> fallback.
    """
    try:
        cands = get_image_candidates(
            kind="poi",
            place_name=(place_name or "").strip(),
            city_name=(city_name or "").strip(),
            country_name=(country_name or "").strip(),
            place_id=(place_id or "").strip(),
            poi_type=(poi_type or "").strip(),
            seed=f"poi_{place_id or place_name}_{city_name}_{country_name}",
            lat=lat,
            lon=lon,
            w=w,
            h=h,
        )
        return (cands[0].get("url") if cands else "") or ""
    except Exception:
        return ""


def get_hotel_image(
    hotel_name: str,
    city_name: str = "",
    country_name: str = "",
    place_id: str = "",
    lat: float = None,
    lon: float = None,
    w: int = 1100,
    h: int = 640,
) -> str:
    """Backward-compatible wrapper: return a single image URL for a hotel/stay."""
    try:
        cands = get_image_candidates(
            kind="hotel",
            place_name=(hotel_name or "").strip(),
            city_name=(city_name or "").strip(),
            country_name=(country_name or "").strip(),
            place_id=(place_id or "").strip(),
            seed=f"hotel_{place_id or hotel_name}_{city_name}_{country_name}",
            lat=lat,
            lon=lon,
            w=w,
            h=h,
        )
        return (cands[0].get("url") if cands else "") or ""
    except Exception:
        return ""

def get_city_hero_image(city_name: str, country_name: str = "") -> str:
    """Wide hero image (PPT cover/banner friendly)."""
    city = (city_name or '').strip()
    country = (country_name or '').strip()
    city = _norm_city_key(city)

    # ✅ deterministic override first
    if city in CITY_HERO_FILES:
        url = _commons_filepath(CITY_HERO_FILES.get(city, ''), width=width)
        _CACHE[f'cityhero:{city}:{country}'] = url
        return url
    if (not city) and country in COUNTRY_HERO_OVERRIDES:
        url = COUNTRY_HERO_OVERRIDES[country]
        _CACHE[f'cityhero:{city}:{country}'] = url
        return url


    cache_key = f"cityimg:v2:hero:{city}|{country}"
    if cache_key in _CACHE:
        return _CACHE[cache_key] or ""

    # 1) Wikimedia thumbnail (best-effort)
    try:
        queries = []
        if city and country:
            queries += [f"{city}, {country}", f"{city} skyline", f"{city} {country} skyline", f"{city} city", city]
        else:
            queries += [f"{city} skyline", f"{city} city", city]
        for q in [x for x in queries if x]:
            meta = _wikimedia_thumb_meta(q, thumb_size=1600, timeout=6)
            if meta and meta.get("thumb_url"):
                url = meta["thumb_url"]
                _CACHE[cache_key] = url
                return url
    except Exception:
        pass

    # 2) Unsplash (fallback)
    try:
        q = f"{city} {country} skyline".strip() if country else f"{city} skyline"
        url = unsplash_pick_image(q, seed=f"hero_{city}_{country}", w=1600, h=900)
        if url:
            _CACHE[cache_key] = url
            return url
    except Exception:
        pass

    url = _picsum(f"hero_{city}_{country}", 1600, 900)
    _CACHE[cache_key] = url
    return url