"""Local POI dataset recommender.

This module lets the app recommend POIs from a user-provided CSV dataset
(e.g., 통합.csv) instead of calling Geoapify for every plan.

What it does
 - Build an in-memory index by (country, city, typ)
 - Train (and cache) a lightweight ranking model (Ridge regression)
 - Pick popular POIs: high rating + high review_count (Bayesian-adjusted)
 - De-duplicate by place_id and normalized name
 - Support Michelin restaurant prioritization
 - Support Landmark attraction prioritization

The dataset is expected to contain at least these columns:
  country, city, feature, name, lat, lon, place_id, place_url, google_rating, google_review_count
"""

from __future__ import annotations

import csv
import json
import math
import os
import re
import time
import hashlib
import unicodedata
import requests
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Set
from urllib.parse import quote


def _picsum_url(seed: str, w: int = 640, h: int = 420) -> str:
    """Deterministic placeholder image URL (no API key needed)."""
    try:
        sid = hashlib.md5((seed or 'x').encode('utf-8', errors='ignore')).hexdigest()
    except Exception:
        sid = 'x'
    return f"https://picsum.photos/seed/{sid}/{int(w)}/{int(h)}"

def _candidate_image_url(place_id: str, seed: str, w: int = 640) -> str:
    """Return image URL for UI cards.

    - If Google Places key exists and IMAGE_PROVIDER allows it -> use internal /api/place_photo proxy with place_id.
    - Otherwise -> use a local placeholder SVG (never random images).
    """
    provider = (os.getenv("IMAGE_PROVIDER") or "auto").strip().lower()
    has_key = bool((os.getenv("GOOGLE_PLACES_API_KEY") or "").strip())
    pid = (place_id or "").strip()
    if has_key and pid and provider in ("auto", "google_places", "google", "g", "places"):
        return f"/api/place_photo/?place_id={quote(pid)}&w={int(w)}"
    return "/static/img/placeholder.svg"




# ============================================================
# Michelin Restaurant Index
# ============================================================

# 한국어 도시명 → 영어 도시명 매핑 (미슐랭 데이터 매칭용)
CITY_NAME_MAP = {
    # 유럽
    "파리": "Paris", "런던": "London", "바르셀로나": "Barcelona", "마드리드": "Madrid",
    "로마": "Rome", "밀라노": "Milan", "피렌체": "Florence", "베네치아": "Venice",
    "베를린": "Berlin", "뮌헨": "Munich", "프랑크푸르트": "Frankfurt",
    "암스테르담": "Amsterdam", "브뤼셀": "Brussels", "빈": "Vienna",
    "프라하": "Prague", "부다페스트": "Budapest", "바르샤바": "Warsaw",
    "리스본": "Lisbon", "아테네": "Athens", "이스탄불": "Istanbul",
    "취리히": "Zurich", "제네바": "Geneva", "코펜하겐": "Copenhagen",
    "스톡홀름": "Stockholm", "오슬로": "Oslo", "헬싱키": "Helsinki",
    "더블린": "Dublin", "에든버러": "Edinburgh",
    # 아시아
    "도쿄": "Tokyo", "오사카": "Osaka", "교토": "Kyoto", "나고야": "Nagoya",
    "후쿠오카": "Fukuoka", "삿포로": "Sapporo", "고베": "Kobe", "요코하마": "Yokohama",
    "서울": "Seoul", "부산": "Busan", "제주": "Jeju", "제주도": "Jeju",
    "홍콩": "Hong Kong", "마카오": "Macau", "타이베이": "Taipei",
    "싱가포르": "Singapore", "방콕": "Bangkok", "호치민": "Ho Chi Minh City",
    "하노이": "Hanoi", "다낭": "Da Nang", "쿠알라룸푸르": "Kuala Lumpur",
    "자카르타": "Jakarta", "발리": "Bali", "뭄바이": "Mumbai", "델리": "Delhi",
    "상하이": "Shanghai", "베이징": "Beijing", "광저우": "Guangzhou", "선전": "Shenzhen",
    # 미주
    "뉴욕": "New York", "로스앤젤레스": "Los Angeles", "샌프란시스코": "San Francisco",
    "시카고": "Chicago", "라스베가스": "Las Vegas", "마이애미": "Miami",
    "워싱턴": "Washington", "보스턴": "Boston", "시애틀": "Seattle",
    "토론토": "Toronto", "밴쿠버": "Vancouver", "몬트리올": "Montreal",
    "멕시코시티": "Mexico City",
    # 오세아니아
    "시드니": "Sydney", "멜버른": "Melbourne", "브리즈번": "Brisbane",
    "퍼스": "Perth", "오클랜드": "Auckland",
    # 중동/아프리카
    "두바이": "Dubai", "아부다비": "Abu Dhabi", "케이프타운": "Cape Town",
}

@dataclass
class MichelinRecord:
    name: str
    grade: str  # "3_star", "2_star", "1_star", "bib_gourmand"
    locality: str
    region: str
    address: str
    lat: Optional[float] = None
    lon: Optional[float] = None
    place_id: str = ""
    url: str = ""


class MichelinIndex:
    """In-memory Michelin restaurant index for priority recommendations."""

    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        self._index: Dict[str, List[MichelinRecord]] = {}  # key: normalized city/locality
        self._loaded = False
        self._city_center_cache: Dict[Tuple[str, str], Tuple[Optional[float], Optional[float]]] = {}
        # Michelin augmentation cache: (country, city) -> list[POIRecord]
        self._michelin_augmented: Dict[Tuple[str, str], List[POIRecord]] = {}

    def _resolve_path(self) -> str:
        # Prefer locally matched dataset (place_id/lat/lon) if present.
        matched_csv = os.path.join(self.base_dir, "data", "michelin_restaurants_matched.csv")
        legacy_csv = os.path.join(self.base_dir, "data", "michelin_restaurants3.csv")
        json_path = os.path.join(self.base_dir, "data", "michelin_overrides.json")
        csv_path = os.path.join(self.base_dir, "data", "michelin_restaurants.csv")

        if os.path.exists(matched_csv):
            return matched_csv
        if os.path.exists(legacy_csv):
            return legacy_csv
        if os.path.exists(json_path):
            return json_path
        return csv_path


    def load(self) -> None:
        if self._loaded:
            return
        self._loaded = True

        path = self._resolve_path()
        if not os.path.exists(path):
            return

        records: List[MichelinRecord] = []

        if path.endswith(".json"):
            try:
                with open(path, "r", encoding="utf-8-sig") as f:
                    data = json.load(f)
                for item in data:
                    rec = MichelinRecord(
                        name=(item.get("name") or "").strip(),
                        grade=(item.get("grade") or "").strip(),
                        locality=(item.get("locality") or "").strip(),
                        region=(item.get("region") or "").strip(),
                        address=(item.get("address") or "").strip(),
                        lat=_safe_float(item.get("lat")),
                        lon=_safe_float(item.get("lon")),
                        place_id=(item.get("place_id") or "").strip(),
                        url=(item.get("url") or "").strip(),
                    )
                    if rec.name and rec.locality:
                        records.append(rec)
            except Exception:
                pass
        else:
            try:
                with open(path, "r", encoding="utf-8-sig", newline="") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        rec = MichelinRecord(
                            name=(row.get("name") or "").strip(),
                            grade=(row.get("grade") or "").strip(),
                            locality=(row.get("locality") or "").strip(),
                            region=(row.get("region") or "").strip(),
                            address=(row.get("address") or "").strip(),
                            lat=_safe_float(row.get("lat")),
                            lon=_safe_float(row.get("lon")),
                            place_id=(row.get("place_id") or "").strip(),
                            url=(row.get("url") or "").strip(),
                        )
                        if rec.name and rec.locality:
                            records.append(rec)
            except Exception:
                pass

        # Build index by normalized locality (영어, 소문자)
        for rec in records:
            key = _norm_key(rec.locality)
            self._index.setdefault(key, []).append(rec)

        # Sort each bucket by grade (3_star > 2_star > 1_star > bib_gourmand)
        grade_priority = {"3_star": 0, "2_star": 1, "1_star": 2, "bib_gourmand": 3}
        for key, lst in self._index.items():
            lst.sort(key=lambda x: grade_priority.get(x.grade, 99))

    def query(self, city: str, limit: int = 4) -> List[MichelinRecord]:
        """Return top Michelin restaurants for a city."""
        self.load()
        
        # 한국어 도시명 → 영어 도시명 변환
        city_en = CITY_NAME_MAP.get(city.strip(), city)
        key = _norm_key(city_en)
        
        results = list(self._index.get(key, []))
        return results[:limit]

    def get_grade_label(self, grade: str) -> str:
        """Return human-readable grade label."""
        labels = {
            "3_star": "⭐⭐⭐ 미슐랭 3스타",
            "2_star": "⭐⭐ 미슐랭 2스타",
            "1_star": "⭐ 미슐랭 1스타",
            "bib_gourmand": "🍴 빕 구르망",
        }
        return labels.get(grade, "미슐랭")

    def get_grade_emoji(self, grade: str) -> str:
        """Return emoji for grade."""
        emojis = {
            "3_star": "🌟🌟🌟",
            "2_star": "🌟🌟",
            "1_star": "🌟",
            "bib_gourmand": "🍴",
        }
        return emojis.get(grade, "🍽️")


# ============================================================
# Landmark Detection Utilities
# ============================================================
LANDMARK_KEYWORDS_EN = {
    "tower", "palace", "castle", "cathedral", "basilica", "temple", "shrine",
    "museum", "gallery", "bridge", "gate", "square", "plaza", "monument",
    "statue", "memorial", "park", "garden", "zoo", "aquarium", "observatory",
    "market", "harbour", "harbor", "beach", "falls", "waterfall", "mountain",
    "opera", "theatre", "theater", "stadium", "arena", "landmark", "famous",
    "historic", "heritage", "unesco", "world heritage", "national", "royal",
    "imperial", "ancient", "old town", "downtown", "city center"
}

LANDMARK_KEYWORDS_KR = {
    "궁", "궁전", "성", "타워", "탑", "전망대", "대성당", "성당", "사찰", "절",
    "신사", "박물관", "미술관", "다리", "교량", "문", "광장", "동상", "기념관",
    "공원", "정원", "동물원", "수족관", "해변", "폭포", "산", "시장", "항구",
    "오페라", "극장", "경기장", "명소", "유적", "유네스코", "세계유산", "국립",
    "왕립", "고궁", "한옥", "전통", "역사", "구시가지"
}

# Feature 값으로 랜드마크 직접 판별 (데이터셋 feature 컬럼 값)
LANDMARK_FEATURES = {"명소", "역사", "박물관/미술관", "광장", "공원/정원", "해변", "자연/힐링"}

# Optional per-city landmark seed overrides.
# File: <BASE_DIR>/data/landmark_seeds.json
# Format:
# [
#   {"country":"대한민국","city":"서울","place_ids":[...],"names":[...]}
# ]
_LANDMARK_SEED_CACHE = None
_LANDMARK_SEED_CACHE_MTIME = None
_LANDMARK_SEED_CACHE_BASE = None


def _load_landmark_seeds(base_dir: str) -> Dict[Tuple[str, str], Dict[str, Set[str]]]:
    """Load landmark seed overrides.

    Returns:
      {(country_key, city_key): {"place_ids": set(...), "names": set(...)}}
    """
    global _LANDMARK_SEED_CACHE, _LANDMARK_SEED_CACHE_MTIME, _LANDMARK_SEED_CACHE_BASE
    try:
        path = os.path.join(base_dir, "data", "landmark_seeds.json")
        if not os.path.exists(path):
            return {}
        mtime = os.path.getmtime(path)
        if (
            _LANDMARK_SEED_CACHE is not None
            and _LANDMARK_SEED_CACHE_MTIME == mtime
            and _LANDMARK_SEED_CACHE_BASE == base_dir
        ):
            return _LANDMARK_SEED_CACHE

        with open(path, "r", encoding="utf-8-sig") as f:
            raw = json.load(f)

        mp: Dict[Tuple[str, str], Dict[str, Set[str]]] = {}
        for item in (raw or []):
            ctry = _norm_key(item.get("country") or "")
            cty = _norm_key(item.get("city") or "")
            if not ctry or not cty:
                continue
            pids = set()
            for pid in (item.get("place_ids") or []):
                try:
                    pid = str(pid or "").strip()
                    if pid:
                        pids.add(pid)
                except Exception:
                    continue
            names = set()
            for nm in (item.get("names") or []):
                try:
                    nm = str(nm or "").strip()
                    if nm:
                        names.add(_norm_key(nm))
                except Exception:
                    continue
            mp[(ctry, cty)] = {"place_ids": pids, "names": names}

        _LANDMARK_SEED_CACHE = mp
        _LANDMARK_SEED_CACHE_MTIME = mtime
        _LANDMARK_SEED_CACHE_BASE = base_dir
        return mp
    except Exception:
        return {}


def _seed_is_landmark(seed_map: Dict[Tuple[str, str], Dict[str, Set[str]]], country: str, city: str, place_id: str, name: str) -> bool:
    try:
        key = (_norm_key(country), _norm_key(city))
        bag = seed_map.get(key) or {}
        pid = (place_id or "").strip()
        if pid and pid in (bag.get("place_ids") or set()):
            return True
        nm = _norm_key(name or "")
        if nm and nm in (bag.get("names") or set()):
            return True
        return False
    except Exception:
        return False


def is_landmark(name: str, feature: str = "") -> bool:
    """Check if a POI looks like a famous landmark."""
    # 먼저 feature 값으로 직접 판별 (가장 정확함)
    feat = (feature or "").strip()
    if feat in LANDMARK_FEATURES:
        return True
    
    combined = f"{name} {feature}".lower()
    
    # Check English keywords
    for kw in LANDMARK_KEYWORDS_EN:
        if kw in combined:
            return True
    
    # Check Korean keywords
    for kw in LANDMARK_KEYWORDS_KR:
        if kw in combined:
            return True
    
    return False


def calculate_landmark_score(name: str, feature: str, rating: float, reviews: int) -> float:
    """Calculate a score that prioritizes landmarks with high engagement."""
    base_score = rating * 2.0 + math.log1p(reviews) * 0.5
    
    if is_landmark(name, feature):
        # Boost for landmarks
        base_score += 5.0
        
        # Extra boost for very popular landmarks (high reviews)
        if reviews > 10000:
            base_score += 3.0
        elif reviews > 5000:
            base_score += 2.0
        elif reviews > 1000:
            base_score += 1.0
    
    return base_score


def _norm_key(s: str) -> str:
    return re.sub(r"\s+", "", (s or "").strip().lower())


def _norm_match_name(s: str) -> str:
    """Aggressive normalization for cross-dataset name matching."""
    s = (s or "").strip()
    if not s:
        return ""
    try:
        s = unicodedata.normalize("NFKD", s)
        s = "".join(ch for ch in s if not unicodedata.combining(ch))
    except Exception:
        pass
    s = s.lower()
    s = re.sub(r"[^a-z0-9가-힣]+", "", s)
    return s


def _safe_float(x, default=None):
    try:
        return float(x)
    except Exception:
        return default


def _safe_int(x, default=0):
    try:
        return int(float(x))
    except Exception:
        return default


def _stable_hash_int(text: str, seed: str = "") -> int:
    try:
        return int(hashlib.md5(f"{seed}:{text}".encode("utf-8", errors="ignore")).hexdigest(), 16)
    except Exception:
        return 0


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    R = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(min(1.0, math.sqrt(a)))


def feature_to_typ(feature: str) -> str:
    """Map dataset feature -> app typ (Attraction/Restaurant/Cafe).

    User preference: pubs/bars are treated as Restaurant.
    """
    s = (feature or "").strip().lower()
    if not s:
        return "Attraction"

    # Cafe
    if any(k in s for k in ["카페", "cafe", "coffee", "베이커", "bakery", "tea", "디저트", "dessert"]):
        return "Cafe"

    # Restaurant (incl. pub/bar)
    if any(k in s for k in [
        "맛집", "식당", "음식", "restaurant", "dining", "catering.restaurant",
        "펍", "바", "pub", "bar", "brew", "beer", "izakaya"
    ]):
        return "Restaurant"

    # Default
    return "Attraction"




# ============================================================
# Known POI misclassifications (dataset cleanup)
# ============================================================
# Some datasets occasionally mislabel famous attractions as cafes/restaurants.
# We patch a few high-impact cases here to keep the UI clean.
_POI_OVERRIDES_BY_NAME = {
    # Seoul
    _norm_key("Bukchon Hanok Village"): {"feature": "명소", "typ": "Attraction", "is_landmark": True},
    _norm_key("북촌한옥마을"): {"feature": "명소", "typ": "Attraction", "is_landmark": True},
}


def _apply_poi_overrides(name: str, feature: str) -> Tuple[str, Optional[str], bool]:
    """Return (feature, forced_typ, forced_landmark)."""
    try:
        k = _norm_key(name or "")
        ov = _POI_OVERRIDES_BY_NAME.get(k)
        if not ov:
            return feature, None, False
        return (ov.get("feature") or feature), (ov.get("typ") or None), bool(ov.get("is_landmark"))
    except Exception:
        return feature, None, False


def _bayesian_weighted_rating(ratings: List[float], reviews: List[int], m_quantile: float = 0.60) -> Tuple[float, float]:
    """Return (C, m) where:
    - C: global mean rating
    - m: minimum reviews threshold (quantile)
    """
    rs = [r for r in ratings if r is not None]
    if not rs:
        return 4.2, 100.0
    C = sum(rs) / max(1, len(rs))
    vs = sorted([max(0, int(v)) for v in reviews])
    if not vs:
        return C, 100.0
    idx = int(max(0, min(len(vs) - 1, round(m_quantile * (len(vs) - 1)))))
    m = float(vs[idx])
    m = max(30.0, min(500.0, m))
    return C, m


@dataclass
class POIRecord:
    country: str
    city: str
    feature: str
    typ: str
    name: str
    lat: float
    lon: float
    place_id: str
    place_url: str
    rating: float
    reviews: int
    score: float
    is_landmark: bool = False
    michelin_grade: str = ""


class LocalPOIIndex:
    """In-memory POI index with a lightweight ranking model."""

    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        self.dataset_path = self._resolve_dataset_path()
        self.model_path = os.path.join(base_dir, "models", "poi_ranker.joblib")
        self._index: Dict[Tuple[str, str, str], List[POIRecord]] = {}
        self._loaded = False
        self._city_center_cache: Dict[Tuple[str, str], Tuple[Optional[float], Optional[float]]] = {}
        # Michelin augmentation cache: (country, city) -> list[POIRecord]
        self._michelin_augmented: Dict[Tuple[str, str], List[POIRecord]] = {}

    # ---------------- env/config ----------------
    def enabled(self) -> bool:
        v = (os.getenv("USE_LOCAL_POI_DATASET") or "0").strip().lower()
        return v in ("1", "true", "yes", "on")

    def min_rating(self) -> float:
        return float((os.getenv("LOCAL_POI_MIN_RATING") or "4.0").strip() or 4.0)

    def min_reviews(self) -> int:
        return int(float((os.getenv("LOCAL_POI_MIN_REVIEWS") or "50").strip() or 50))

    def radius_km(self) -> float:
        return float((os.getenv("LOCAL_POI_RADIUS_KM") or "45").strip() or 35.0)

    def top_pool(self) -> int:
        return int(float((os.getenv("LOCAL_POI_TOP_POOL") or "200").strip() or 120))

    def _resolve_dataset_path(self) -> str:
        p = (os.getenv("LOCAL_POI_DATA_PATH") or "").strip()
        if p:
            if not os.path.isabs(p):
                p = os.path.join(self.base_dir, p)
            return p
        return os.path.join(self.base_dir, "data", "poi_dataset.csv")


    def get_city_center(self, country: str, city: str) -> Tuple[Optional[float], Optional[float]]:
        """Return a robust (lat, lon) center for a city from the loaded dataset.

        Purpose
        - Make `radius_km` filtering meaningful (avoid far-outliers / wrong neighborhoods)
        - Keep results stable across runs by using a robust statistic (median)

        Notes
        - If the dataset doesn't contain the city, returns (None, None)
        - Result is cached per (country, city) for speed.
        """
        self.load()
        key = (_norm_key(country), _norm_key(city))
        # If country is empty, try to infer it from the dataset (only when unambiguous)
        if not key[0] and key[1]:
            matches = set()
            for (ck, cy, _t) in (self._index or {}).keys():
                if cy == key[1] and ck:
                    matches.add(ck)
            if len(matches) == 1:
                key = (next(iter(matches)), key[1])
        if key in self._city_center_cache:
            return self._city_center_cache[key]

        lats: List[float] = []
        lons: List[float] = []

        # Gather across common types for better coverage
        for typ in ("Attraction", "Restaurant", "Cafe"):
            for rec in (self._index.get((key[0], key[1], typ)) or []):
                try:
                    lats.append(float(rec.lat))
                    lons.append(float(rec.lon))
                except Exception:
                    continue

        # Fallback: any remaining buckets
        if not lats:
            for (ck, cy, _t), lst in (self._index or {}).items():
                if ck == key[0] and cy == key[1]:
                    for rec in lst:
                        try:
                            lats.append(float(rec.lat))
                            lons.append(float(rec.lon))
                        except Exception:
                            continue

        if not lats:
            self._city_center_cache[key] = (None, None)
            return (None, None)

        lats.sort()
        lons.sort()
        mid = len(lats) // 2
        center = (lats[mid], lons[mid])
        self._city_center_cache[key] = center
        return center

    # ---------------- loading + ranking ----------------
    def _train_or_load_model(self, rows: List[Dict[str, Any]]):
        """Train a simple Ridge ranker and cache via joblib."""
        try:
            import joblib
            from sklearn.pipeline import Pipeline
            from sklearn.feature_extraction import DictVectorizer
            from sklearn.linear_model import Ridge
            from sklearn.exceptions import InconsistentVersionWarning
            import warnings

            os.makedirs(os.path.dirname(self.model_path), exist_ok=True)

            if os.path.exists(self.model_path):
                try:
                    # Avoid noisy version mismatch warnings when sklearn was upgraded.
                    # If loading actually fails, we will fall back to retraining below.
                    with warnings.catch_warnings():
                        warnings.filterwarnings("ignore", category=InconsistentVersionWarning)
                        return joblib.load(self.model_path)
                except Exception:
                    pass

            # Build training data
            ratings = [float(r.get("google_rating") or 0) for r in rows]
            reviews = [int(float(r.get("google_review_count") or 0)) for r in rows]
            C, m = _bayesian_weighted_rating(ratings, reviews)

            X = []
            y = []
            for r in rows:
                R = _safe_float(r.get("google_rating"), 0.0) or 0.0
                v = _safe_int(r.get("google_review_count"), 0)
                if R <= 0 or v <= 0:
                    continue
                wr = (v / (v + m)) * R + (m / (v + m)) * C
                target = float(wr) * math.log1p(max(0, v))

                typ = feature_to_typ(r.get("feature") or "")
                X.append({
                    "rating": float(R),
                    "log_reviews": math.log1p(max(0, v)),
                    "typ": typ,
                    "country": (r.get("country") or "").strip(),
                    "city": (r.get("city") or "").strip(),
                })
                y.append(target)

            if len(X) < 50:
                # Not enough data, return None and fallback to heuristic.
                return None

            pipe = Pipeline([
                ("vec", DictVectorizer(sparse=True)),
                ("model", Ridge(alpha=1.0, random_state=0)),
            ])
            pipe.fit(X, y)
            joblib.dump(pipe, self.model_path)
            return pipe
        except Exception:
            return None

    def load(self) -> None:
        if self._loaded:
            return
        self._loaded = True

        if not self.enabled():
            return
        if not self.dataset_path or (not os.path.exists(self.dataset_path)):
            return

        rows: List[Dict[str, Any]] = []
        try:
            with open(self.dataset_path, "r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                for r in reader:
                    rows.append(r)
        except Exception:
            return

        # Train/load ranker
        ranker = self._train_or_load_model(rows)

        # De-dup by place_id (best) else (name+lat+lon)
        seen_pid = set()
        seen_fallback = set()

        # Heuristic constants
        ratings = [float(r.get("google_rating") or 0) for r in rows]
        reviews = [int(float(r.get("google_review_count") or 0)) for r in rows]
        C, m = _bayesian_weighted_rating(ratings, reviews)

        # Seed overrides (optional)
        seed_map = _load_landmark_seeds(self.base_dir)

        for r in rows:
            country = (r.get("country") or "").strip()
            city = (r.get("city") or "").strip()
            # 컬럼명 호환: google_name 또는 name
            name = (r.get("google_name") or r.get("name") or "").strip()
            feature = (r.get("feature") or "").strip()
            # 컬럼명 호환: google_lat/google_lon 또는 lat/lon
            lat = _safe_float(r.get("google_lat") or r.get("lat"))
            lon = _safe_float(r.get("google_lon") or r.get("lon"))
            if not country or not city or not name or lat is None or lon is None:
                continue

            pid = (r.get("place_id") or "").strip()
            purl = (r.get("place_url") or "").strip()
            rating = _safe_float(r.get("google_rating"), 0.0) or 0.0
            reviews_n = _safe_int(r.get("google_review_count"), 0)
            if rating <= 0:
                continue

            if pid:
                if pid in seen_pid:
                    continue
                seen_pid.add(pid)
            else:
                fb = f"{_norm_key(name)}:{round(lat,6)}:{round(lon,6)}"
                if fb in seen_fallback:
                    continue
                seen_fallback.add(fb)
            # Apply known dataset cleanup overrides (e.g., landmark mislabels)
            feature, forced_typ, forced_landmark = _apply_poi_overrides(name, feature)
            typ = forced_typ or feature_to_typ(feature)

            # Score: model prediction if available, else Bayesian*log(reviews)
            score = 0.0
            if ranker is not None:
                try:
                    score = float(ranker.predict([{
                        "rating": float(rating),
                        "log_reviews": math.log1p(max(0, reviews_n)),
                        "typ": typ,
                        "country": country,
                        "city": city,
                    }])[0])
                except Exception:
                    score = 0.0
            if score == 0.0:
                v = max(0, reviews_n)
                wr = (v / (v + m)) * float(rating) + (m / (v + m)) * float(C)
                score = float(wr) * math.log1p(v)

            # Check if this is a landmark (for Attractions)
            is_lm = False
            if typ == "Attraction":
                is_lm = is_landmark(name, feature)
                # If heuristics didn't catch it, try per-city seed overrides
                if (not is_lm) and _seed_is_landmark(seed_map, country, city, pid, name):
                    is_lm = True
            # Forced landmark override (dataset cleanup)
            if forced_landmark:
                is_lm = True

            rec = POIRecord(
                country=country,
                city=city,
                feature=feature,
                typ=typ,
                name=name,
                lat=float(lat),
                lon=float(lon),
                place_id=pid,
                place_url=purl,
                rating=float(rating),
                reviews=int(reviews_n),
                score=float(score),
                is_landmark=is_lm,
                michelin_grade="",
            )

            k = (_norm_key(country), _norm_key(city), typ)
            self._index.setdefault(k, []).append(rec)

        # Sort each bucket by score desc
        for k, lst in self._index.items():
            lst.sort(key=lambda x: (-x.score, -x.reviews, -x.rating))



    # ---------------- michelin augmentation ----------------
    def _google_places_textsearch(self, query: str, lat: float = None, lon: float = None, radius_m: int = 20000, timeout: int = 8) -> Optional[Dict[str, Any]]:
        """Google Places Text Search (legacy) returning geometry/rating.

        Returns dict with keys: place_id, name, lat, lon, rating, reviews, photo_reference
        Uses PlacesCache to reduce repeated calls.
        """
        key = (os.getenv("GOOGLE_PLACES_API_KEY") or "").strip()
        if not key or not query:
            return None

        # Persistent cache
        try:
            from storage.place_cache import PlacesCache
            qttl_days = int(float(os.getenv("PLACE_QUERY_TTL_DAYS", "14")))
            pttl_days = int(float(os.getenv("PLACE_DETAILS_TTL_DAYS", "14")))
            qttl = max(1, min(30, qttl_days)) * 24 * 60 * 60
            pttl = max(1, min(30, pttl_days)) * 24 * 60 * 60
            db = PlacesCache(base_dir=self.base_dir)
            pid = db.get_place_id_for_query(query, lat=lat, lon=lon, radius_m=radius_m)
            if pid:
                cached = db.get_place(pid) or {}
                if cached.get("lat") is not None and cached.get("lon") is not None:
                    return {
                        "place_id": cached.get("place_id") or pid,
                        "name": cached.get("name") or "",
                        "lat": cached.get("lat"),
                        "lon": cached.get("lon"),
                        "rating": None,
                        "reviews": None,
                        "photo_reference": cached.get("photo_reference") or "",
                    }
        except Exception:
            db = None
            qttl = pttl = None

        url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
        params: Dict[str, Any] = {"query": query, "key": key, "language": "en"}
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
            pid = (best.get("place_id") or "").strip()
            nm = (best.get("name") or "").strip()
            loc = ((best.get("geometry") or {}).get("location") or {})
            plat = _safe_float(loc.get("lat"))
            plon = _safe_float(loc.get("lng"))
            rating = _safe_float(best.get("rating"))
            reviews = _safe_int(best.get("user_ratings_total"), 0)
            photos = best.get("photos") or []
            pref = (photos[0] or {}).get("photo_reference") if photos else ""

            out = {
                "place_id": pid,
                "name": nm,
                "lat": plat,
                "lon": plon,
                "rating": rating,
                "reviews": reviews,
                "photo_reference": (pref or "").strip(),
            }

            # Save to cache best-effort
            try:
                from storage.place_cache import PlacesCache
                qttl_days = int(float(os.getenv("PLACE_QUERY_TTL_DAYS", "14")))
                pttl_days = int(float(os.getenv("PLACE_DETAILS_TTL_DAYS", "14")))
                qttl = max(1, min(30, qttl_days)) * 24 * 60 * 60
                pttl = max(1, min(30, pttl_days)) * 24 * 60 * 60
                db = PlacesCache(base_dir=self.base_dir)
                if pid:
                    db.set_query_mapping(query, pid, ttl_seconds=qttl, lat=lat, lon=lon, radius_m=radius_m)
                    db.set_place(pid, name_en=nm, photo_reference=out.get("photo_reference") or "", lat=plat, lon=plon, ttl_seconds=pttl)
            except Exception:
                pass

            return out
        except Exception:
            return None


    def _augment_michelin_restaurants(self, country: str, city: str, center_lat: float = None, center_lon: float = None) -> List[POIRecord]:
        """Inject Michelin restaurants not present in the local dataset.

        Prefer *local matched Michelin dataset* (place_id/lat/lon) to avoid API cost.
        Fallback to Places Text Search only if:
        - Michelin records have no place_id/lat/lon, AND
        - GOOGLE_PLACES_API_KEY is configured.
        """
        key = (_norm_key(country), _norm_key(city))
        if key in self._michelin_augmented:
            return self._michelin_augmented[key]

        out: List[POIRecord] = []

        # Michelin list
        try:
            mi = get_michelin_index(self.base_dir)
            mi.load()
            city_en = CITY_NAME_MAP.get((city or '').strip(), (city or '').strip())
            mk = _norm_key(city_en)
            mi_list = list(mi._index.get(mk) or [])
        except Exception:
            mi_list = []

        if not mi_list:
            self._michelin_augmented[key] = out
            return out

        grade_order = {"3_star": 0, "2_star": 1, "1_star": 2, "bib_gourmand": 3, "selected": 4}
        mi_list = sorted(mi_list, key=lambda r: (grade_order.get(r.grade, 9), (r.name or "")))

        existing: Set[str] = set()
        for rec in (self._index.get((_norm_key(country), _norm_key(city), "Restaurant")) or []):
            if rec.place_id:
                existing.add(rec.place_id)

        try:
            max_items = int(float(os.getenv("MICHELIN_AUGMENT_MAX_CALLS", "12")))
        except Exception:
            max_items = 12
        max_items = max(0, min(50, max_items))
        if max_items <= 0:
            self._michelin_augmented[key] = out
            return out

        # ---- 1) Local matched dataset mode (no API key needed) ----
        has_local = any(((r.place_id or "").strip() and r.lat is not None and r.lon is not None) for r in mi_list)
        if has_local:
            try:
                max_dist_m = float(os.getenv("MICHELIN_AUGMENT_MAX_DIST_M", "45000"))
            except Exception:
                max_dist_m = 45000.0

            calls = 0
            for mrec in mi_list:
                if calls >= max_items:
                    break
                pid = (mrec.place_id or "").strip()
                if not pid or pid in existing:
                    continue
                if mrec.lat is None or mrec.lon is None:
                    continue

                # Optional distance guard (keeps out-of-city matches from slipping in)
                if center_lat is not None and center_lon is not None:
                    try:
                        d_m = _haversine_km(float(center_lat), float(center_lon), float(mrec.lat), float(mrec.lon)) * 1000.0
                        if d_m > max_dist_m:
                            continue
                    except Exception:
                        pass

                # Local-only: we don't have live rating/reviews without API calls.
                rating = 4.7
                reviews = 150
                base_score = float(rating) * math.log1p(max(0, int(reviews)))

                boost = 1.5
                if (mrec.grade or "") == "3_star":
                    boost = 8.0
                elif (mrec.grade or "") == "2_star":
                    boost = 5.0
                elif (mrec.grade or "") == "1_star":
                    boost = 3.0
                else:
                    boost = 1.5

                out.append(POIRecord(
                    country=country,
                    city=city,
                    feature="맛집",
                    typ="Restaurant",
                    name=(mrec.name or "").strip() or "Michelin Restaurant",
                    lat=float(mrec.lat),
                    lon=float(mrec.lon),
                    place_id=pid,
                    place_url=f"https://www.google.com/maps/search/?api=1&query_place_id={quote(pid)}",
                    rating=float(rating),
                    reviews=int(reviews),
                    score=float(base_score + boost),
                    is_landmark=False,
                    michelin_grade=(mrec.grade or "").strip(),
                ))
                existing.add(pid)
                calls += 1

            self._michelin_augmented[key] = out
            return out

        # ---- 2) Fallback: Places Text Search (API key required) ----
        if not (os.getenv("GOOGLE_PLACES_API_KEY") or "").strip():
            self._michelin_augmented[key] = out
            return out

        bias_lat = center_lat
        bias_lon = center_lon

        calls = 0
        for mrec in mi_list:
            if calls >= max_items:
                break
            nm = (mrec.name or "").strip()
            if not nm:
                continue

            q = f"{nm} {CITY_NAME_MAP.get((city or '').strip(), (city or '').strip())}"
            res = self._google_places_textsearch(q, lat=bias_lat, lon=bias_lon, radius_m=25000)
            if not res:
                continue
            pid = (res.get("place_id") or "").strip()
            if not pid or pid in existing:
                continue
            plat = res.get("lat")
            plon = res.get("lon")
            if plat is None or plon is None:
                continue

            rating = res.get("rating")
            reviews = res.get("reviews")
            try:
                rating = float(rating) if rating is not None else 4.6
            except Exception:
                rating = 4.6
            try:
                reviews = int(reviews) if reviews is not None else 50
            except Exception:
                reviews = 50

            base_score = float(rating) * math.log1p(max(0, reviews))
            boost = 1.5
            if (mrec.grade or "") == "3_star":
                boost = 8.0
            elif (mrec.grade or "") == "2_star":
                boost = 5.0
            elif (mrec.grade or "") == "1_star":
                boost = 3.0
            else:
                boost = 1.5

            out.append(POIRecord(
                country=country,
                city=city,
                feature="맛집",
                typ="Restaurant",
                name=(res.get("name") or nm),
                lat=float(plat),
                lon=float(plon),
                place_id=pid,
                place_url=f"https://www.google.com/maps/search/?api=1&query_place_id={quote(pid)}",
                rating=float(rating),
                reviews=int(reviews),
                score=float(base_score + boost),
                is_landmark=False,
                michelin_grade=(mrec.grade or "").strip(),
            ))
            existing.add(pid)
            calls += 1

        self._michelin_augmented[key] = out
        return out


    # ---------------- query ----------------
    def query(
        self,
        country: str,
        city: str,
        typ: str,
        center_lat: float,
        center_lon: float,
        limit: int = 10,
        exclude_names_lower: Optional[set] = None,
        day_seed: str = "",
        # optional overrides (app may pass these)
        min_rating: Optional[float] = None,
        min_reviews: Optional[int] = None,
        radius_km: Optional[float] = None,
        top_pool: Optional[int] = None,
        exclude: Optional[set] = None,
    ) -> List[Dict[str, Any]]:
        """Return ranked candidates in the app's expected schema.

        Strategy
        - Pre-filter by (rating, review_count, radius_km)
        - Build a *diversified* candidate pool using a simple geo-grid (cell_km)
          before cutting to `top_pool` so big cities don't collapse into one neighborhood
        - Return up to `limit` items, de-duplicated
        """
        self.load()
        if not self.enabled() or not self._index:
            return []

        exclude_names_lower = exclude_names_lower or set()
        if exclude:
            try:
                exclude_names_lower = set(exclude_names_lower) | set(exclude)
            except Exception:
                pass

        # Normalize excludes to catch spacing/punctuation variants (e.g., "뉴서울호텔" vs "뉴 서울 호텔")
        exclude_norm = set()
        for x in (exclude_names_lower or set()):
            try:
                xs = str(x or "").strip().lower()
                if not xs:
                    continue
                exclude_norm.add(xs)
                exclude_norm.add(_norm_key(xs))
                exclude_norm.add(re.sub(r"[^a-z0-9가-힣]+", "", xs))
            except Exception:
                continue

        k = (_norm_key(country), _norm_key(city), typ)
        lst = list(self._index.get(k) or [])
        if not lst:
            return []

        # ---- Michelin labeling (Restaurant only) ----
        if typ == 'Restaurant':
            try:
                mi = get_michelin_index(self.base_dir)
                mi.load()
                city_en = CITY_NAME_MAP.get((city or '').strip(), (city or '').strip())
                mk = _norm_key(city_en)
                mrecs = list(mi._index.get(mk) or [])

                # 1) name-based exact-ish match (normalized)
                michelin_by_name = {}
                michelin_geo = []
                for mrec in mrecs:
                    try:
                        nk = _norm_match_name(mrec.name)
                        if nk and (mrec.grade or ''):
                            michelin_by_name[nk] = (mrec.grade or '').strip()
                    except Exception:
                        pass
                    try:
                        if mrec.lat is not None and mrec.lon is not None and (mrec.grade or ''):
                            michelin_geo.append((float(mrec.lat), float(mrec.lon), (mrec.grade or '').strip()))
                    except Exception:
                        pass

                # 2) geo-nearest match (helps KR/EN name mismatch, e.g., 서울)
                match_km = 0.35
                try:
                    match_km = float((os.getenv("MICHELIN_MATCH_KM") or "0.35").strip() or 0.35)
                except Exception:
                    match_km = 0.35

                for rec in lst:
                    try:
                        if rec.michelin_grade:
                            continue
                        nk = _norm_match_name(rec.name)
                        g = michelin_by_name.get(nk)
                        if g:
                            rec.michelin_grade = g
                            continue
                    except Exception:
                        pass

                    # geo match
                    try:
                        if (not rec.michelin_grade) and michelin_geo and (rec.lat is not None) and (rec.lon is not None):
                            best_g = None
                            best_d = None
                            for (mlat, mlon, g) in michelin_geo:
                                d = _haversine_km(float(rec.lat), float(rec.lon), float(mlat), float(mlon))
                                if best_d is None or d < best_d:
                                    best_d = d
                                    best_g = g
                            if best_d is not None and best_d <= match_km and best_g:
                                rec.michelin_grade = best_g
                    except Exception:
                        pass



                # 3) If the local dataset has too few Michelin restaurants, try to augment from Michelin list via Google Places.
                try:
                    min_cnt = int(float(os.getenv("MICHELIN_AUGMENT_MIN_COUNT", "6")))
                except Exception:
                    min_cnt = 6
                try:
                    cur_cnt = sum(1 for rr in lst if (rr.michelin_grade or "").strip())
                    if cur_cnt < max(1, min_cnt):
                        aug = self._augment_michelin_restaurants(country=country, city=city, center_lat=center_lat, center_lon=center_lon)
                        # Append augmented records (dedupe by place_id)
                        seen = set((r.place_id or "") for r in lst if (r.place_id or "").strip())
                        for a in (aug or []):
                            pid = (a.place_id or "").strip()
                            if pid and pid not in seen:
                                lst.append(a)
                                seen.add(pid)
                except Exception:
                    pass

            except Exception:
                # never block recommendation on michelin map failures
                pass

        eff_min_rating = float(min_rating) if min_rating is not None else self.min_rating()
        eff_min_reviews = int(min_reviews) if min_reviews is not None else self.min_reviews()
        eff_radius_km = float(radius_km) if radius_km is not None else self.radius_km()
        eff_top_pool = max(60, int(top_pool) if top_pool is not None else self.top_pool())

        # Pre-filter (rating/reviews + exclude)  (radius is applied after with auto-expansion)
        base: List[POIRecord] = []
        for rec in lst:
            if rec.rating < eff_min_rating:
                continue
            if rec.reviews < eff_min_reviews:
                continue

            pid = (rec.place_id or "").strip()
            nm = (rec.name or "").strip()
            if pid and (f"pid:{pid}" in exclude_norm):
                continue
            if nm:
                nm_l = nm.lower()
                if nm_l in exclude_norm:
                    continue
                if _norm_key(nm_l) in exclude_norm:
                    continue
                nm_alnum = re.sub(r"[^a-z0-9가-힣]+", "", nm_l)
                if nm_alnum in exclude_norm:
                    continue
            base.append(rec)

        if not base:
            return []

        # Apply radius filter (if center provided), but auto-expand if it becomes too restrictive.
        filtered: List[POIRecord] = list(base)
        target_n = max(12, int(limit) if limit is not None else 12)

        if center_lat is not None and center_lon is not None:
            # Precompute distances once
            dist_pairs: List[Tuple[POIRecord, Optional[float]]] = []
            for rec in base:
                try:
                    dist_pairs.append((rec, _haversine_km(center_lat, center_lon, rec.lat, rec.lon)))
                except Exception:
                    dist_pairs.append((rec, None))

            rad = float(eff_radius_km) if eff_radius_km is not None else 0.0
            if rad <= 0:
                rad = 45.0
            max_rad = max(80.0, rad * 3.0)

            # expand radius until we have enough candidates (or hit cap)
            while True:
                filtered = [r for (r, d) in dist_pairs if (d is None) or (d <= rad)]
                if len(filtered) >= target_n:
                    break
                if rad >= max_rad:
                    break
                rad = min(max_rad, rad * 1.6)

            # If still too few, fall back to closest-by-distance pool
            if len(filtered) < min(target_n, 6):
                dd = [(r, d) for (r, d) in dist_pairs if d is not None]
                dd.sort(key=lambda x: x[1])
                take = min(len(dd), max(target_n, 60))
                filtered = [r for (r, d) in dd[:take]] or list(base)

        if not filtered:
            return []

        # ⭐ 핵심 수정: 랜드마크를 최우선으로! 리뷰 수(인기도) 기준
        # Attraction의 경우 랜드마크를 먼저 분리해서 리뷰 수 기준으로 정렬
        if typ == "Attraction":
            landmarks = [r for r in filtered if r.is_landmark]
            non_landmarks = [r for r in filtered if not r.is_landmark]
            
            # 랜드마크: 리뷰 수(인기도) 기준 내림차순 정렬 - 가장 유명한 것 먼저!
            landmarks.sort(key=lambda r: (-r.reviews, -r.rating, -r.score))
            # 비랜드마크: score 기준
            non_landmarks.sort(key=lambda r: (-r.score, -r.reviews, -r.rating))
            
            # 랜드마크 먼저, 그 다음 비랜드마크
            filtered = landmarks + non_landmarks
        else:
            # Sort once (score desc), keep deterministic shuffle for ties
            filtered.sort(key=lambda r: (-r.score, -r.reviews, -r.rating, _stable_hash_int(r.place_id or r.name, seed=f"pre:{day_seed}")))

        # --- Diversify candidate pool by grid before cutting ---
        def _grid_km_for_type(t: str) -> float:
            try:
                if t == "Attraction":
                    return float((os.getenv("LOCAL_POI_GRID_KM_ATTR") or "3.5").strip() or 3.5)
                if t == "Restaurant":
                    return float((os.getenv("LOCAL_POI_GRID_KM_REST") or "2.2").strip() or 2.2)
                if t == "Cafe":
                    return float((os.getenv("LOCAL_POI_GRID_KM_CAFE") or "1.8").strip() or 1.8)
            except Exception:
                pass
            return 3.0

        cell_km = _grid_km_for_type(typ)
        # Don't make grid too tiny/huge
        try:
            cell_km = float(cell_km)
        except Exception:
            cell_km = 3.0
        cell_km = max(0.8, min(cell_km, 8.0))

        # Convert km -> degree steps (approx)
        try:
            lat_step = max(1e-6, cell_km / 110.574)
            cosv = math.cos(math.radians(float(center_lat or 0.0) or 0.0))
            lon_step = max(1e-6, cell_km / (111.320 * (cosv if abs(cosv) > 1e-3 else 1.0)))
        except Exception:
            lat_step, lon_step = (0.03, 0.03)

        def _cell_key(latv: float, lonv: float) -> Tuple[int, int]:
            return (int(math.floor(float(latv) / lat_step)), int(math.floor(float(lonv) / lon_step)))

        buckets: Dict[Tuple[int, int], List[POIRecord]] = {}
        for r in filtered:
            try:
                ck = _cell_key(r.lat, r.lon)
            except Exception:
                ck = (0, 0)
            buckets.setdefault(ck, []).append(r)

        # Within each cell: keep best-first
        for ck, arr in buckets.items():
            arr.sort(key=lambda r: (-r.score, -r.reviews, -r.rating, _stable_hash_int(r.place_id or r.name, seed=f"cell:{day_seed}:{ck}")))

        # Order cells by their top score (and deterministic tiebreak)
        cell_order = list(buckets.keys())
        cell_order.sort(key=lambda ck: (-(buckets[ck][0].score if buckets.get(ck) else 0.0), _stable_hash_int(str(ck), seed=f"cells:{day_seed}")))

        # Round-robin pick from cells to build diversified pool
        pool_target = min(len(filtered), max(eff_top_pool, int(limit * 12)))
        pool: List[POIRecord] = []
        ptr = 0
        # safety loop cap
        safety = 0
        while len(pool) < pool_target and safety < (pool_target * 10 + 200):
            safety += 1
            if not cell_order:
                break
            ck = cell_order[ptr % len(cell_order)]
            arr = buckets.get(ck) or []
            if arr:
                pool.append(arr.pop(0))
            else:
                # remove empty cell
                try:
                    cell_order.remove(ck)
                except Exception:
                    pass
                continue
            ptr += 1

        if not pool:
            pool = filtered[:pool_target]

        # ⭐ 최종 정렬: Attraction은 랜드마크 우선 + 리뷰 수 기준
        if typ == "Attraction":
            pool.sort(key=lambda r: (
                0 if r.is_landmark else 1,  # 랜드마크 먼저
                -r.reviews,  # 리뷰 수 많은 순
                -r.rating,
                _stable_hash_int(r.place_id or r.name, seed=f"cand:{day_seed}")
            ))
        else:
            # Final deterministic ordering inside the pool (still mostly score-driven)
            pool.sort(key=lambda r: (-r.score, _stable_hash_int(r.place_id or r.name, seed=f"cand:{day_seed}")))

        out: List[Dict[str, Any]] = []
        used_norm = set()
        for rec in pool:
            if len(out) >= limit:
                break
            nm = (rec.name or "").strip()
            nm_norm = re.sub(r"[^a-z0-9가-힣]+", "", nm.lower())
            if nm_norm and nm_norm in used_norm:
                continue
            used_norm.add(nm_norm)

            out.append({
                "id": f"ds:{typ}:{rec.place_id or hashlib.md5(nm.encode('utf-8', errors='ignore')).hexdigest()}",
                "google_place_id": rec.place_id or "",
                "type": typ,
                "name": nm,
                "raw_name": nm,
                "rating": rec.rating,
                "image": _candidate_image_url(rec.place_id or "", rec.place_id or nm, 640),
                "image_hd": _candidate_image_url(rec.place_id or "", rec.place_id or nm, 1600),
                "latlng": (rec.lat, rec.lon),
                "distance_km": round(_haversine_km(center_lat, center_lon, rec.lat, rec.lon), 2)
                if (center_lat is not None and center_lon is not None)
                else None,
                "place_url": rec.place_url,
                "feature": rec.feature,
                "reviews": rec.reviews,
                "score": rec.score,
                "is_landmark": rec.is_landmark,
                "michelin_grade": rec.michelin_grade,
            })

        return out


_LOCAL_INDEX: Optional[LocalPOIIndex] = None
_MICHELIN_INDEX: Optional[MichelinIndex] = None


def get_local_index(base_dir: str) -> LocalPOIIndex:
    global _LOCAL_INDEX
    if _LOCAL_INDEX is None:
        _LOCAL_INDEX = LocalPOIIndex(base_dir=base_dir)
    return _LOCAL_INDEX


def get_michelin_index(base_dir: str) -> MichelinIndex:
    global _MICHELIN_INDEX
    if _MICHELIN_INDEX is None:
        _MICHELIN_INDEX = MichelinIndex(base_dir=base_dir)
    return _MICHELIN_INDEX