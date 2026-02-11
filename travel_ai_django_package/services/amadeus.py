"""
Amadeus API Service
- 공항 검색
- 호텔 검색 (by city code)
- 호텔 가격 조회
"""
import os
import time
import requests
from typing import Any, Dict, List, Optional

AMADEUS_KEY = os.getenv("Amadeus_Hotel_Search_API", "").strip()
AMADEUS_SECRET = os.getenv("Amadeus_Hotel_Search_API_SECRET", "").strip()
AMADEUS_BASE_URL = os.getenv("AMADEUS_BASE_URL", "https://test.api.amadeus.com").strip().rstrip("/")

_token_cache: Dict[str, Any] = {"access_token": None, "expires_at": 0}


def _has_keys() -> bool:
    return bool(AMADEUS_KEY and AMADEUS_SECRET)


def get_access_token() -> Optional[str]:
    """OAuth 토큰 획득"""
    if not _has_keys():
        return None

    now = int(time.time())
    if _token_cache.get("access_token") and now < int(_token_cache.get("expires_at", 0)) - 30:
        return _token_cache["access_token"]

    url = f"{AMADEUS_BASE_URL}/v1/security/oauth2/token"
    data = {
        "grant_type": "client_credentials",
        "client_id": AMADEUS_KEY,
        "client_secret": AMADEUS_SECRET,
    }
    
    try:
        r = requests.post(url, data=data, timeout=10, 
                         headers={"Content-Type": "application/x-www-form-urlencoded"})
        r.raise_for_status()
        js = r.json()
        token = js.get("access_token")
        expires_in = int(js.get("expires_in", 1800))
        _token_cache["access_token"] = token
        _token_cache["expires_at"] = now + expires_in
        return token
    except Exception as e:
        print(f"Amadeus token error: {e}")
        return None


def _auth_headers() -> Dict[str, str]:
    token = get_access_token()
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


def search_locations(keyword: str, sub_type: str = "CITY", 
                    country_code: str = "", limit: int = 10) -> List[Dict[str, Any]]:
    """
    공항/도시 검색
    sub_type: CITY, AIRPORT, CITY,AIRPORT
    """
    if not _has_keys():
        return []

    keyword = (keyword or "").strip()
    if not keyword:
        return []

    # Amadeus location search는 한글 키워드에 대해 400을 반환하는 경우가 많습니다.
    # (프로젝트 UI가 한글 도시명을 사용하기 때문에) 여기서는 조용히 skip 처리합니다.
    # 비행시간 계산은 app.py의 영문 매핑/대체 로직에서 이어서 처리됩니다.
    try:
        keyword.encode("ascii")
    except Exception:
        return []

    url = f"{AMADEUS_BASE_URL}/v1/reference-data/locations"
    params: Dict[str, Any] = {
        "keyword": keyword,
        "subType": sub_type,
        "page[limit]": limit,
    }
    cc = (country_code or "").strip().upper()
    if cc:
        params["countryCode"] = cc

    try:
        r = requests.get(url, params=params, timeout=10, headers=_auth_headers())
        r.raise_for_status()
        data = r.json().get("data", [])
    except Exception as e:
        print(f"Amadeus locations error: {e}")
        return []

    out: List[Dict[str, Any]] = []
    for it in data:
        address = it.get("address", {}) or {}
        geo = it.get("geoCode", {}) or {}
        out.append({
            "name": it.get("name") or "",
            "detailedName": it.get("detailedName") or it.get("name") or "",
            "iataCode": it.get("iataCode") or "",
            "subType": it.get("subType") or "",
            "cityName": address.get("cityName") or "",
            "countryName": address.get("countryName") or "",
            "countryCode": address.get("countryCode") or "",
            "lat": geo.get("latitude"),
            "lon": geo.get("longitude"),
        })
    return out


def hotels_by_city(city_code: str, limit: int = 12) -> List[Dict[str, Any]]:
    """IATA 도시 코드로 호텔 목록 조회 (PAR, TYO 등)"""
    if not _has_keys():
        return []
    
    city_code = (city_code or "").strip().upper()
    if not city_code:
        return []
    
    url = f"{AMADEUS_BASE_URL}/v1/reference-data/locations/hotels/by-city"
    params = {"cityCode": city_code, "radius": 20, "radiusUnit": "KM"}
    
    try:
        r = requests.get(url, params=params, timeout=12, headers=_auth_headers())
        r.raise_for_status()
        data = r.json().get("data", [])
    except Exception as e:
        print(f"Amadeus hotels error: {e}")
        return []

    out: List[Dict[str, Any]] = []
    for it in data[:limit]:
        geo = it.get("geoCode", {}) or {}
        out.append({
            "hotelId": it.get("hotelId") or "",
            "name": it.get("name") or "",
            "lat": geo.get("latitude"),
            "lon": geo.get("longitude"),
        })
    return out


def hotel_offers(hotel_ids: List[str], adults: int, check_in: str, 
                check_out: str, limit: int = 12) -> Dict[str, Any]:
    """호텔 가격 조회"""
    if not _has_keys() or not hotel_ids:
        return {}

    url = f"{AMADEUS_BASE_URL}/v3/shopping/hotel-offers"
    params = {
        "hotelIds": ",".join(hotel_ids[:30]),
        "adults": max(1, int(adults)),
        "checkInDate": check_in,
        "checkOutDate": check_out,
        "roomQuantity": 1,
        "bestRateOnly": True,
        "view": "FULL",
    }
    
    try:
        r = requests.get(url, params=params, timeout=15, headers=_auth_headers())
        r.raise_for_status()
        js = r.json()
    except Exception as e:
        print(f"Amadeus offers error: {e}")
        return {}

    offers = {}
    for h in js.get("data", [])[:limit]:
        hid = (h.get("hotel") or {}).get("hotelId") or ""
        price = None
        for off in h.get("offers", []) or []:
            p = (off.get("price") or {}).get("total")
            if p is None:
                continue
            try:
                val = float(p)
            except:
                val = None
            if val is not None:
                if price is None or val < price[0]:
                    price = (val, p)
        if hid and price:
            currency = ((h.get("offers", [{}])[0].get("price", {}) or {}).get("currency"))
            offers[hid] = {"total": price[1], "currency": currency}
    
    return offers


def _parse_iso_duration_to_minutes(iso: str) -> Optional[int]:
    """ISO8601 duration like 'PT2H35M' -> minutes"""
    if not iso or not isinstance(iso, str):
        return None
    try:
        iso = iso.strip().upper()
        if not iso.startswith("PT"):
            return None
        iso = iso[2:]
        h = 0
        m = 0
        num = ""
        for ch in iso:
            if ch.isdigit():
                num += ch
            else:
                if ch == "H":
                    h = int(num or "0")
                elif ch == "M":
                    m = int(num or "0")
                num = ""
        return h * 60 + m
    except Exception:
        return None

def flight_offers_duration(origin_code: str, dest_code: str, departure_date: str, adults: int = 1) -> Dict[str, Any]:
    """Amadeus Flight Offers Search로 비행시간(최소 1개 오퍼) 가져오기.
    origin_code/dest_code는 IATA (CITY or AIRPORT) 코드를 받음.
    """
    origin_code = (origin_code or "").strip().upper()
    dest_code = (dest_code or "").strip().upper()
    if not origin_code or not dest_code or not departure_date:
        return {"ok": False, "error": "missing params"}

    url = f"{AMADEUS_BASE_URL}/v2/shopping/flight-offers"
    params: Dict[str, Any] = {
        "originLocationCode": origin_code,
        "destinationLocationCode": dest_code,
        "departureDate": departure_date,
        "adults": max(1, int(adults or 1)),
        "max": 1,
        "currencyCode": "KRW",
    }

    try:
        r = requests.get(url, params=params, timeout=20, headers=_auth_headers())
        r.raise_for_status()
        js = r.json()
    except Exception as e:
        return {"ok": False, "error": f"flight-offers error: {e}"}

    data = (js.get("data") or [])
    if not data:
        return {"ok": False, "error": "no offers"}

    offer = data[0]
    itin = (offer.get("itineraries") or [])
    if not itin:
        return {"ok": False, "error": "no itineraries"}

    duration_iso = itin[0].get("duration") or ""
    duration_min = _parse_iso_duration_to_minutes(duration_iso)
    segs = (itin[0].get("segments") or [])
    carriers = []
    for s in segs:
        c = (s.get("carrierCode") or "").strip()
        if c and c not in carriers:
            carriers.append(c)

    return {
        "ok": True,
        "duration_iso": duration_iso,
        "duration_min": duration_min,
        "segments": len(segs),
        "carriers": carriers
    }
