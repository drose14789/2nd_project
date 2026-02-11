"""
Geoapify API Service
- Autocomplete (도시/국가 검색)
- Places API (POI 검색)
- Hotels (숙소 검색)
"""
import os
import time
import requests
from typing import List, Dict, Any, Optional

GEOAPIFY_KEY = (os.getenv("Geoapify_Places_API") or os.getenv("GEOAPIFY_PLACES_API_KEY") or os.getenv("GEOAPIFY_API_KEY") or "").strip()

# 국가명 한글 매핑
KOREAN_COUNTRY_NAME = {
    "KR": "대한민국", "JP": "일본", "US": "미국", "GB": "영국",
    "FR": "프랑스", "ES": "스페인", "IT": "이탈리아", "DE": "독일",
    "CN": "중국", "TW": "대만", "TH": "태국", "VN": "베트남",
    "SG": "싱가포르", "AU": "호주", "CA": "캐나다", "HK": "홍콩",
    "PH": "필리핀", "ID": "인도네시아", "MY": "말레이시아",
}

def _short_city_name(name: str) -> str:
    """행정구역 접미사 제거 (서울특별시 -> 서울, 도쿄도 -> 도쿄 등)"""
    if not name:
        return ""
    s = str(name).strip()
    # common Korean admin suffixes
    for suf in ["특별시", "광역시"]:
        if s.endswith(suf) and len(s) > len(suf):
            s = s[: -len(suf)]
            return s
    # single-char suffixes frequently returned for JP like 도/현
    for suf in ["도", "현", "부", "주"]:
        if s.endswith(suf) and len(s) > 1:
            # avoid stripping country names like '호주'
            if s == "호주":
                break
            s = s[: -len(suf)]
            return s
    # city-level suffix (오사카시 등)
    for suf in ["시", "군", "구"]:
        if s.endswith(suf) and len(s) > 2:
            s = s[: -len(suf)]
            return s
    return s


def _nominatim_search(text: str, limit: int = 8, country_code: str = "") -> List[Dict[str, Any]]:
    """OpenStreetMap Nominatim fallback (no API key).
    Note: demo 용으로만 사용. (rate limit 있음)
    """
    try:
        url = "https://nominatim.openstreetmap.org/search"
        params = {
            "q": text,
            "format": "json",
            "limit": str(limit),
            "addressdetails": 1,
        }
        headers = {"User-Agent": "TravelAI/1.0 (demo)" , "Accept-Language": "ko"}
        r = requests.get(url, params=params, timeout=8, headers=headers)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"Nominatim error: {e}")
        return []

    out: List[Dict[str, Any]] = []
    for it in data:
        addr = it.get("address") or {}
        code = (addr.get("country_code") or "").upper()
        if country_code and code and code != country_code.upper():
            continue

        country = addr.get("country") or ""
        kr_country = KOREAN_COUNTRY_NAME.get(code, country) if code else country

        # pick best city-like field
        city = addr.get("city") or addr.get("town") or addr.get("village") or addr.get("county") or it.get("display_name","")
        city = city.split(",")[0].strip()
        short_city = _short_city_name(city)

        out.append({
            "label": short_city or city,
            "city": short_city or city,
            "country": kr_country or country,
            "country_code": code,
            "lat": float(it.get("lat") or 0),
            "lon": float(it.get("lon") or 0),
            "kind": "city",
            "source": "nominatim",
        })
    return out

# 캐시
_CACHE: Dict[str, Any] = {}
_CACHE_TTL = 600  # 10분


def _cache_get(k: str):
    v = _CACHE.get(k)
    if not v:
        return None
    if time.time() - v["t"] > _CACHE_TTL:
        _CACHE.pop(k, None)
        return None
    return v["v"]


def _cache_set(k: str, v: Any):
    _CACHE[k] = {"t": time.time(), "v": v}


def autocomplete_place(text: str, limit: int = 8, kind: str = "city_fallback", 
                       country_code: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Geoapify Geocoding autocomplete
    kind: city, country, any, city_fallback
    """
    text = (text or "").strip()
    if not text or not GEOAPIFY_KEY:
        return []

    cc = (country_code or "").strip().upper()
    cache_key = f"ac:{kind}:{cc}:{text}:{limit}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    url = "https://api.geoapify.com/v1/geocode/autocomplete"

    def _call(params: Dict[str, Any]) -> List[Dict[str, Any]]:
        try:
            r = requests.get(url, params=params, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"Geoapify error: {e}")
            return []

        out: List[Dict[str, Any]] = []
        for item in data.get("results", []):
            city = (
                item.get("city") or item.get("name") or item.get("suburb") or
                item.get("district") or item.get("county") or item.get("state") or
                item.get("country") or ""
            )
            country = item.get("country") or ""
            code = (item.get("country_code") or "").upper()
            
            # 한글 국가명
            kr_country = KOREAN_COUNTRY_NAME.get(code, country)
            
            place_type = "city" if item.get("city") else (
                "country" if (country and (city == country or item.get("result_type") == "country")) 
                else "other"
            )

            if place_type == "country":
                label = kr_country or country or city
            else:
                short_city = _short_city_name(city)
            label = short_city or city if kr_country else city

            out.append({
                "label": label,
                "city": (_short_city_name(city) or city),
                "country": country,
                "country_kr": kr_country,
                "country_code": code,
                "place_type": place_type,
                "lat": item.get("lat"),
                "lon": item.get("lon"),
            })
        return out

    # Geoapify Autocomplete 기본 파라미터
    # - lang 는 화면 표시 언어/키워드 매칭에 영향을 주므로, 프로젝트에서는 영어(en)로 통일
    #   (일본어/혼합 표기 방지 + 결과 안정성)
    base_params: Dict[str, Any] = {
        "text": text,
        "limit": limit,
        "format": "json",
        "lang": "en",
        "apiKey": GEOAPIFY_KEY,
    }
    if cc:
        base_params["filter"] = f"countrycode:{cc.lower()}"

    results: List[Dict[str, Any]] = []

    if kind == "city":
        base_params["type"] = "city"
        results = _call(base_params)
    elif kind == "country":
        base_params["type"] = "country"
        results = _call(base_params)
    elif kind == "any":
        results = _call(base_params)
    else:  # city_fallback
        base_params["type"] = "city"
        results = _call(base_params)
        if not results:
            del base_params["type"]
            results = _call(base_params)
        # Korean/local name fallback (Geoapify가 한글 도시명을 못 찾는 경우 대비)
        if not results:
            results = _nominatim_search(text=text, limit=limit, country_code=cc or "")

    _cache_set(cache_key, results)
    return results


def autocomplete_city(text: str, limit: int = 8) -> List[Dict[str, Any]]:
    """호환성 래퍼"""
    return autocomplete_place(text=text, limit=limit, kind="city_fallback")


def places_near(lat: float, lon: float, categories: str, limit: int = 10, 
                radius_m: int = 5000) -> List[Dict[str, Any]]:
    """
    Geoapify Places API - POI 검색
    categories: tourism.attraction, catering.restaurant, catering.cafe 등
    """
    if not GEOAPIFY_KEY:
        return []

    cache_key = f"places:{lat:.4f}:{lon:.4f}:{categories}:{limit}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    url = "https://api.geoapify.com/v2/places"
    params: Dict[str, Any] = {
        "categories": categories,
        "filter": f"circle:{lon},{lat},{radius_m}",
        "bias": f"proximity:{lon},{lat}",
        "limit": limit,
        "apiKey": GEOAPIFY_KEY,
        "lang": "en",
    }
    
    try:
        r = requests.get(url, params=params, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"Places API error: {e}")
        return []

    out = []
    for f in data.get("features", []):
        p = f.get("properties", {})
        geom = f.get("geometry", {})
        coords = geom.get("coordinates") or [None, None]
        out.append({
            "id": p.get("place_id") or p.get("name") or "poi",
            "name": p.get("name") or p.get("formatted") or "POI",
            "address": p.get("formatted") or p.get("address_line2") or "",
            "country": p.get("country") or "",
            "city": p.get("city") or "",
            "lat": coords[1],
            "lon": coords[0],
            "categories": p.get("categories") or [],
        })
    
    _cache_set(cache_key, out)
    return out


def hotels_near(lat: float, lon: float, limit: int = 10, radius_m: int = 7000) -> List[Dict[str, Any]]:
    """Geoapify로 숙소(호텔/호스텔/게스트하우스/모텔/샬레/아파트 등) 검색.

    기본은 호텔만이 아니라 *accommodation* 전체를 대상으로 하며,
    환경변수 ACCOMMODATION_CATEGORIES 로 카테고리를 커스터마이즈할 수 있습니다.

    참고: Geoapify Places API Supported categories - Accommodation
      accommodation, accommodation.hotel, accommodation.hostel, accommodation.guest_house,
      accommodation.motel, accommodation.apartment, accommodation.chalet, accommodation.hut
    """
    cats = (os.getenv("ACCOMMODATION_CATEGORIES") or "").strip()
    if not cats:
        cats = "accommodation.hotel,accommodation.hostel,accommodation.guest_house,accommodation.motel,accommodation.apartment,accommodation.chalet,accommodation.hut"
    return places_near(lat, lon, categories=cats, limit=limit, radius_m=radius_m)


def airports_near(lat: float, lon: float, limit: int = 5, radius_m: int = 30000):
    """도시 주변 공항(Place) 검색"""
    try:
        # Geoapify Places API는 `aeroway.aerodrome` 를 지원하지 않아 400 오류가 납니다.
        # 대신 공식 카테고리인 `airport` 를 사용합니다.
        return places_near(lat, lon, categories="airport", limit=limit, radius_m=radius_m)
    except Exception:
        return []
