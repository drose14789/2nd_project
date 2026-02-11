
from __future__ import annotations

import os
import json
import time
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, List

import requests

_HTML_TAG_RE = re.compile(r"<[^>]+>")

def _strip_html(s: str) -> str:
    if not s:
        return ""
    return _HTML_TAG_RE.sub("", s).replace("&nbsp;", " ").strip()

def _now() -> int:
    return int(time.time())

def _project_root() -> str:
    # services/ is under the Django project root.
    return str(Path(__file__).resolve().parents[1])

def _instance_dir(base_dir: Optional[str] = None) -> str:
    if base_dir is None:
        base_dir = _project_root()
    p = Path(base_dir) / "instance"
    p.mkdir(parents=True, exist_ok=True)
    return str(p)

def _cache_path(base_dir: Optional[str] = None) -> str:
    return str(Path(_instance_dir(base_dir)) / "transit_cache.json")

def _load_cache(base_dir: Optional[str] = None) -> Dict[str, Any]:
    p = _cache_path(base_dir)
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}

def _save_cache(cache: Dict[str, Any], base_dir: Optional[str] = None) -> None:
    p = _cache_path(base_dir)
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except Exception:
        pass

def _get_google_maps_key() -> Optional[str]:
    # Prefer GOOGLE_MAPS_API_KEY, fallback to GOOGLE_PLACES_API_KEY for convenience.
    return (os.getenv("GOOGLE_MAPS_API_KEY") or os.getenv("GOOGLE_PLACES_API_KEY") or "").strip() or None

def build_transit_fallback_url(origin: Tuple[float, float], dest: Tuple[float, float]) -> str:
    olat, olon = origin
    dlat, dlon = dest
    return f"https://www.google.com/maps/dir/?api=1&origin={olat},{olon}&destination={dlat},{dlon}&travelmode=transit"

def get_transit_route(
    origin: Tuple[float, float],
    dest: Tuple[float, float],
    departure_time: Optional[int] = None,
    language: str = "ko",
    base_dir: Optional[str] = None,
    ttl_days: int = 7,
) -> Optional[Dict[str, Any]]:
    """Return best-effort public transit route via Google Directions API (mode=transit) with caching.

    Returns:
      {
        duration_min, distance_km, steps:[...], provider:'google'
      }
    """
    key = _get_google_maps_key()
    if not key:
        return None

    try:
        olat, olon = float(origin[0]), float(origin[1])
        dlat, dlon = float(dest[0]), float(dest[1])
    except Exception:
        return None

    # cache key (rounded + hourly bucket)
    if departure_time is None:
        departure_time = _now()
    bucket = int(int(departure_time) // 3600)
    ck = f"{round(olat,5)},{round(olon,5)}->{round(dlat,5)},{round(dlon,5)}|{bucket}|{language}"

    cache = _load_cache(base_dir)
    item = cache.get(ck)
    if item:
        ts = int(item.get("ts") or 0)
        if ts and (_now() - ts) < int(ttl_days) * 86400:
            data = item.get("data")
            if isinstance(data, dict):
                return data

    url = "https://maps.googleapis.com/maps/api/directions/json"
    params = {
        "origin": f"{olat},{olon}",
        "destination": f"{dlat},{dlon}",
        "mode": "transit",
        "language": language or "ko",
        "departure_time": int(departure_time),
        "key": key,
    }

    try:
        r = requests.get(url, params=params, timeout=15)
        j = r.json() if r is not None else {}
    except Exception:
        return None

    if not isinstance(j, dict) or j.get("status") not in ("OK", "ZERO_RESULTS"):
        return None
    if j.get("status") != "OK":
        return None

    routes = j.get("routes") or []
    if not routes:
        return None
    legs = (routes[0].get("legs") or [])
    if not legs:
        return None
    leg = legs[0] or {}

    dist_m = (leg.get("distance") or {}).get("value") or 0
    dur_s = (leg.get("duration") or {}).get("value") or 0

    steps_out: List[Dict[str, Any]] = []
    for st in (leg.get("steps") or []):
        travel_mode = (st.get("travel_mode") or "").lower()
        if travel_mode == "walking":
            steps_out.append({
                "type": "walk",
                "distance_m": (st.get("distance") or {}).get("value") or 0,
                "duration_min": round(((st.get("duration") or {}).get("value") or 0) / 60, 1),
                "instruction": _strip_html(st.get("html_instructions") or ""),
            })
        elif travel_mode == "transit":
            td = st.get("transit_details") or {}
            line = (td.get("line") or {})
            vehicle = ((line.get("vehicle") or {}).get("type") or "TRANSIT").upper()
            steps_out.append({
                "type": "transit",
                "vehicle": vehicle,
                "line": (line.get("short_name") or line.get("name") or "").strip(),
                "headsign": (td.get("headsign") or "").strip(),
                "num_stops": td.get("num_stops"),
                "departure_stop": ((td.get("departure_stop") or {}).get("name") or "").strip(),
                "arrival_stop": ((td.get("arrival_stop") or {}).get("name") or "").strip(),
                "duration_min": round(((st.get("duration") or {}).get("value") or 0) / 60, 1),
                "instruction": _strip_html(st.get("html_instructions") or ""),
            })
        else:
            steps_out.append({
                "type": "other",
                "duration_min": round(((st.get("duration") or {}).get("value") or 0) / 60, 1),
                "instruction": _strip_html(st.get("html_instructions") or ""),
            })

    out = {
        "provider": "google",
        "distance_km": round(float(dist_m) / 1000.0, 2),
        "duration_min": round(float(dur_s) / 60.0, 1),
        "steps": steps_out,
    }

    cache[ck] = {"ts": _now(), "data": out}
    _save_cache(cache, base_dir)
    return out
