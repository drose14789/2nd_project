"""
OpenRouteService API - 경로/거리 계산
"""
import os
import requests
from typing import List, Tuple, Dict, Any, Optional

ORS_KEY = (os.getenv("openrouteservice_API") or os.getenv("OPENROUTESERVICE_API_KEY") or os.getenv("ORS_API_KEY") or "").strip()


def directions(coords: List[Tuple[float, float]]) -> Optional[Dict[str, Any]]:
    """
    경로 계산 (driving)
    coords: [(lng, lat), ...] 순서
    """
    if not ORS_KEY or len(coords) < 2:
        return None
    
    url = "https://api.openrouteservice.org/v2/directions/driving-car"
    headers = {
        "Authorization": ORS_KEY,
        "Content-Type": "application/json"
    }
    body = {"coordinates": coords}
    
    try:
        r = requests.post(url, json=body, headers=headers, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"ORS error: {e}")
        return None


def summarize_duration_distance(resp: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    """응답에서 총 시간(분)과 거리(km) 추출"""
    if not resp:
        return None, None
    
    try:
        routes = resp.get("routes", [])
        if not routes:
            return None, None
        
        summary = routes[0].get("summary", {})
        duration_sec = summary.get("duration", 0)
        distance_m = summary.get("distance", 0)
        
        return duration_sec / 60, distance_m / 1000
    except Exception:
        return None, None


def get_route_summary(start: Tuple[float, float], end: Tuple[float, float]) -> Optional[Dict]:
    """두 지점 간 경로 요약"""
    # coords는 (lng, lat) 순서
    coords = [(start[1], start[0]), (end[1], end[0])]
    resp = directions(coords)
    
    if resp:
        mins, km = summarize_duration_distance(resp)
        if mins is not None and km is not None:
            return {
                "duration_min": round(mins, 0),
                "distance_km": round(km, 1)
            }
    return None


def directions_geojson(coords: List[Tuple[float, float]]) -> Optional[Dict[str, Any]]:
    """경로 계산 결과를 GeoJSON으로 반환 (라인스트링 좌표 얻기 용)"""
    if not ORS_KEY or len(coords) < 2:
        return None

    url = "https://api.openrouteservice.org/v2/directions/driving-car/geojson"
    headers = {"Authorization": ORS_KEY, "Content-Type": "application/json"}
    body = {"coordinates": coords, "instructions": False}

    try:
        r = requests.post(url, json=body, headers=headers, timeout=12)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None

