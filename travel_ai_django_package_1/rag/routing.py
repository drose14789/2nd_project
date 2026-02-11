"""
rag/routing.py - country/city/topic 라우팅 유틸

- city 라우팅: rag/cities.json의 alias 매칭으로 country/city 추정
- topic 라우팅: 간단 키워드 기반(transport/food/cafe/sights/tips)
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional, Tuple, Dict, Any

_THIS_DIR = Path(__file__).resolve().parent
CITIES_JSON_PATH = _THIS_DIR / "cities.json"

DEFAULT_TOPICS = ("transport", "food", "cafe", "sights", "tips")

_TOPIC_KEYWORDS = {
    "transport": ["교통", "이동", "지하철", "버스", "택시", "공항", "패스", "노선", "환승", "트램", "전철"],
    "food": ["맛집", "음식", "라멘", "스시", "고기", "미슐랭", "레스토랑", "식당", "먹을", "카레", "딤섬"],
    "cafe": ["카페", "베이커리", "디저트", "커피", "빵", "브런치"],
    "sights": ["관광", "명소", "랜드마크", "박물관", "미술관", "전망대", "사원", "궁", "성", "공원", "투어"],
    "tips": ["주의", "팁", "치안", "안전", "환전", "비자", "예절", "물가", "날씨", "준비물"],
}

def _normalize(s: str) -> str:
    s = (s or "").strip()
    return re.sub(r"\s+", " ", s).lower()

def load_cities_map() -> Dict[str, Any]:
    """cities.json 로드. 파일이 없으면 빈 dict."""
    if not CITIES_JSON_PATH.exists():
        return {}
    try:
        return json.loads(CITIES_JSON_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}

def detect_city(query: str) -> Tuple[Optional[str], Optional[str]]:
    """query에서 (country, city) 추정. 없으면 (None, None)."""
    qn = _normalize(query)
    cities = load_cities_map()
    for city_slug, meta in cities.items():
        aliases = meta.get("aliases", []) or []
        # city_slug 자체도 alias로 취급
        candidates = list(aliases) + [city_slug]
        for a in candidates:
            if not a:
                continue
            if _normalize(a) in qn:
                return meta.get("country"), city_slug
    return None, None

def detect_topic(query: str) -> Optional[str]:
    """query에서 topic 추정."""
    q = _normalize(query)
    for topic, kws in _TOPIC_KEYWORDS.items():
        for kw in kws:
            if _normalize(kw) in q:
                return topic
    return None
