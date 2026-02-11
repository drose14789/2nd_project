"""Routing / travel-time utilities.

This project previously used straight-line distance + a fixed speed to estimate
segment travel times. Users asked for more realistic "구간별 이동시간".

We implement a lightweight provider with caching:

- Provider: OSRM public demo (driving) https://router.project-osrm.org
- Cache: JSON file in ``BASE_DIR/instance/route_cache.json``
- Fallback: haversine distance with configurable speeds

Environment variables
---------------------
ROUTING_PROVIDER: osrm | approx (default: osrm)
ROUTE_CACHE_TTL_DAYS: 1..30 (default: 14)
CITY_SPEED_KMH: speed for fallback inside city (default: 18)
INTERCITY_SPEED_KMH: speed for fallback for longer drives (default: 80)
OSRM_TIMEOUT_SEC: http timeout seconds (default: 6)

Notes
-----
OSRM demo may throttle; cache + fallback keep UX stable.
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import requests
from django.conf import settings


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    try:
        R = 6371.0
        phi1 = math.radians(float(lat1))
        phi2 = math.radians(float(lat2))
        dphi = math.radians(float(lat2) - float(lat1))
        dl = math.radians(float(lon2) - float(lon1))
        a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dl / 2) ** 2
        return 2 * R * math.asin(min(1.0, math.sqrt(a)))
    except Exception:
        return 0.0


def _ttl_seconds(default_days: int = 14) -> int:
    try:
        days = int((os.getenv("ROUTE_CACHE_TTL_DAYS") or str(default_days)).strip())
    except Exception:
        days = default_days
    days = max(1, min(30, days))
    return int(days * 24 * 60 * 60)


def _cache_path() -> str:
    cache_dir = os.path.join(str(settings.BASE_DIR), "instance")
    os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(cache_dir, "route_cache.json")


def _load_cache() -> Dict[str, Dict]:
    p = _cache_path()
    try:
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
            if isinstance(data, dict):
                return data
    except Exception:
        return {}
    return {}


def _save_cache(data: Dict[str, Dict]) -> None:
    p = _cache_path()
    try:
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, p)
    except Exception:
        # Cache must never break core UX
        pass


def _key(lat1: float, lon1: float, lat2: float, lon2: float, profile: str) -> str:
    # quantize to reduce key explosion
    def q(x: float) -> str:
        try:
            return f"{float(x):.5f}"
        except Exception:
            return "0.00000"

    return f"{profile}:{q(lat1)},{q(lon1)}->{q(lat2)},{q(lon2)}"


@dataclass
class RouteSegment:
    distance_km: float
    duration_min: float
    provider: str


def _approx_segment(lat1: float, lon1: float, lat2: float, lon2: float) -> RouteSegment:
    km = _haversine_km(lat1, lon1, lat2, lon2)
    try:
        city_speed = float(os.getenv("CITY_SPEED_KMH") or 18)
    except Exception:
        city_speed = 18.0
    try:
        inter_speed = float(os.getenv("INTERCITY_SPEED_KMH") or 80)
    except Exception:
        inter_speed = 80.0

    speed = city_speed if km <= 20 else inter_speed
    mins = (km / max(speed, 1e-6)) * 60.0
    return RouteSegment(distance_km=float(km), duration_min=float(mins), provider="approx")


def _osrm_segment(lat1: float, lon1: float, lat2: float, lon2: float, profile: str = "driving") -> Optional[RouteSegment]:
    # OSRM expects lon,lat
    try:
        timeout = int(os.getenv("OSRM_TIMEOUT_SEC") or 6)
    except Exception:
        timeout = 6

    url = f"https://router.project-osrm.org/route/v1/{profile}/{float(lon1)},{float(lat1)};{float(lon2)},{float(lat2)}"
    params = {
        "overview": "false",
        "alternatives": "false",
        "steps": "false",
    }
    try:
        r = requests.get(url, params=params, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return None
        data = r.json() or {}
        routes = data.get("routes") or []
        if not routes:
            return None
        rt = routes[0] or {}
        dist_m = float(rt.get("distance") or 0.0)
        dur_s = float(rt.get("duration") or 0.0)
        if dist_m <= 0 or dur_s <= 0:
            return None
        return RouteSegment(distance_km=dist_m / 1000.0, duration_min=dur_s / 60.0, provider="osrm")
    except Exception:
        return None


def get_segment(lat1: float, lon1: float, lat2: float, lon2: float, profile: str = "driving") -> RouteSegment:
    """Get distance/time for a segment, with cache + fallback."""
    provider = (os.getenv("ROUTING_PROVIDER") or "osrm").strip().lower()
    if provider not in ("osrm", "approx"):
        provider = "osrm"

    k = _key(lat1, lon1, lat2, lon2, profile)
    ttl = _ttl_seconds(14)
    now = time.time()
    cache = _load_cache()
    try:
        item = cache.get(k) or {}
        ts = float(item.get("ts") or 0.0)
        if ts and (now - ts) <= ttl:
            return RouteSegment(
                distance_km=float(item.get("distance_km") or 0.0),
                duration_min=float(item.get("duration_min") or 0.0),
                provider=str(item.get("provider") or "cache"),
            )
    except Exception:
        pass

    seg: Optional[RouteSegment] = None
    if provider == "osrm":
        seg = _osrm_segment(lat1, lon1, lat2, lon2, profile=profile)
    if seg is None:
        seg = _approx_segment(lat1, lon1, lat2, lon2)

    # Save cache (best-effort)
    try:
        cache[k] = {
            "ts": now,
            "distance_km": float(seg.distance_km),
            "duration_min": float(seg.duration_min),
            "provider": str(seg.provider),
        }
        _save_cache(cache)
    except Exception:
        pass
    return seg
