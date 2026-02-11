"""Geoapify Static Maps helper - route polyline + markers"""
import os
import math
from typing import List, Tuple, Optional
from urllib.parse import quote

GEOAPIFY_KEY = (os.getenv("Geoapify_Places_API") or os.getenv("GEOAPIFY_PLACES_API_KEY") or os.getenv("GEOAPIFY_API_KEY") or "").strip()

def _bbox(points: List[Tuple[float, float]]):
    lons = [p[0] for p in points if p and p[0] is not None]
    lats = [p[1] for p in points if p and p[1] is not None]
    if not lons or not lats:
        return None
    return (min(lons), min(lats), max(lons), max(lats))

def _downsample(points: List[Tuple[float, float]], max_n: int = 90) -> List[Tuple[float, float]]:
    if len(points) <= max_n:
        return points
    step = max(1, len(points)//max_n)
    sampled = points[::step]
    if sampled[-1] != points[-1]:
        sampled.append(points[-1])
    return sampled[:max_n]

def build_route_static_map_url(points_or_route, route_line_lonlat=None, size: str = "900x450", hd: bool = False, width: int = 900, height: int = 450) -> Optional[str]:
    """Geoapify Static Map URL 생성.

    사용처:
      - 결과/Itinerary 화면: day_plan.route + day_plan.route_line
      - PPT: route_map_url_hd

    목표:
      - 웹 결과 화면(Leaflet의 fitBounds)처럼 *루트가 화면을 꽉 채우도록* 줌인
      - 다만 마커/선이 잘리지 않도록 최소 여백 유지

    입력:
      - points_or_route:
          (구버전) [(lon,lat), ...]
          (신버전) [{'name','lat','lng'}, ...]
      - route_line_lonlat: [(lon,lat), ...] (optional)
    """
    if not GEOAPIFY_KEY:
        return None

    # parse size string
    if isinstance(size, str) and "x" in size:
        try:
            w, h = size.lower().split("x")
            width = int(w)
            height = int(h)
        except Exception:
            pass

    route_nodes_lonlat: List[Tuple[float, float]] = []
    line_lonlat: List[Tuple[float, float]] = []

    if isinstance(points_or_route, list) and points_or_route:
        first = points_or_route[0]
        if isinstance(first, dict):
            for n in points_or_route:
                try:
                    lat = n.get('lat')
                    lng = n.get('lng')
                    if lat is None or lng is None:
                        continue
                    route_nodes_lonlat.append((float(lng), float(lat)))
                except Exception:
                    continue
            if route_line_lonlat:
                try:
                    line_lonlat = [(float(p[0]), float(p[1])) for p in route_line_lonlat if p and p[0] is not None and p[1] is not None]
                except Exception:
                    line_lonlat = []
        elif isinstance(first, (tuple, list)) and len(first) == 2:
            route_nodes_lonlat = [(float(p[0]), float(p[1])) for p in points_or_route if p and p[0] is not None and p[1] is not None]

    # draw points: prefer line if provided, else nodes
    draw_pts = line_lonlat if len(line_lonlat) >= 2 else route_nodes_lonlat
    if len(draw_pts) < 2:
        return None

    # bbox source: prefer *route nodes* to mimic UI fitBounds and avoid polyline outliers.
    bbox_src = route_nodes_lonlat if len(route_nodes_lonlat) >= 2 else draw_pts

    pts = _downsample(draw_pts, max_n=90)
    bb = _bbox(bbox_src)
    area = None

    if bb:
        minlon, minlat, maxlon, maxlat = bb
        span_lon_deg = max(1e-6, (maxlon - minlon))
        span_lat_deg = max(1e-6, (maxlat - minlat))
        lat_mean = (minlat + maxlat) / 2.0
        cos_lat = max(0.2, abs(math.cos(math.radians(lat_mean))))

        # degrees -> km (approx)
        lon_km = span_lon_deg * 111.32 * cos_lat
        lat_km = span_lat_deg * 110.574
        span_km = max(lon_km, lat_km)

        # padding (km)
        # hd=True is primarily used for PPT exports: we want a *tighter* bbox (more zoom-in)
        pad_ratio = 0.09
        if span_km > 25:
            pad_ratio = 0.12
        elif span_km < 2:
            pad_ratio = 0.07
        elif span_km < 0.5:
            pad_ratio = 0.05

        min_pad_km = 0.25 if span_km < 1.0 else 0.4
        extra_km = 0.2  # marker/linewidth safety

        if hd:
            # PPT용(HD) 지도는 결과 화면(Leaflet fitBounds)처럼 더 타이트하게 줌인되길 원합니다.
            # padding을 더 줄여(더 줌인)도 마커/라인이 잘리지 않도록 최소 안전여백을 유지합니다.
            # - pad_ratio: 전체 스팬 대비 여백 비율
            # - min_pad_km: 아주 짧은 동선에서의 최소 여백
            # 더 강하게 줌인(사용자 요청: PPT에서 루트가 여전히 작게 보임)
            # 원칙: 비율 기반 여백(pad_ratio)은 최대한 줄이고, 최소 여백(min_pad_km) + 안전여백(extra_km)만 남긴다.
            # - pad_ratio는 상대 여백(장거리에서도 일정 수준의 여백은 필요)
            # - min_pad_km/extra_km는 마커/라인 두께에 대한 절대 안전여백
            pad_ratio = max(0.0025, pad_ratio * 0.08)
            min_pad_km = max(0.05, min_pad_km * 0.28)
            extra_km = 0.10
        pad_km = max(min_pad_km, span_km * pad_ratio) + extra_km
        # 상한 캡은 너무 넓은 여백을 방지. PPT(HD)에서는 더 공격적으로 줄여준다.
        cap_km = max(3.5, span_km * 0.28)
        if hd:
            cap_km = max(2.2, span_km * 0.17)
        pad_km = min(pad_km, cap_km)

        pad_lon = pad_km / (111.32 * cos_lat)
        pad_lat = pad_km / 110.574

        # Ensure the polyline is inside bbox (rare cases where line deviates more than nodes)
        bb_line = _bbox(pts)
        if bb_line:
            lminlon, lminlat, lmaxlon, lmaxlat = bb_line
            minlon = min(minlon, lminlon)
            maxlon = max(maxlon, lmaxlon)
            minlat = min(minlat, lminlat)
            maxlat = max(maxlat, lmaxlat)

        area = f"area=rect:{minlon-pad_lon},{maxlat+pad_lat},{maxlon+pad_lon},{minlat-pad_lat}"

    coord_str = ",".join([f"{lon},{lat}" for lon, lat in pts])
    geom = f"geometry=polyline:{coord_str};linewidth:6;linecolor:%23007bff"

    start = pts[0]
    endp = pts[-1]
    marker = (
        f"marker=lonlat:{start[0]},{start[1]};color:%2300c853;size:large|"
        f"lonlat:{endp[0]},{endp[1]};color:%23d50000;size:large"
    )

    url = f"https://maps.geoapify.com/v1/staticmap?style=osm-carto&width={width}&height={height}&{marker}&{geom}"
    if area:
        url += "&" + area
    url += f"&apiKey={quote(GEOAPIFY_KEY)}"
    return url

