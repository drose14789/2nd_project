"""
Day itinerary explanation generator (LLM + RAG with safe fallback).
- Uses services.rag_engine.search for retrieval
- Uses services.llm_client.generate_explanation_openai for LLM (optional)
- Always returns a usable explanation text.

✅ Fix (2026-02-09):
- Chat "일정 설명해줘"에서 특정 타입(맛집 등)이 설명에서 누락되던 문제를 막기 위해,
  LLM 사용 여부와 무관하게 **현재 일정(순서) 전체를 명시적으로 헤더에 포함**합니다.
"""
from __future__ import annotations

import math
import re
from typing import Dict, Any, List, Optional

from .rag_engine import search as rag_search
from .llm_client import generate_explanation_openai, is_llm_configured


def _canonical_type(t: str) -> str:
    """Normalize various category strings to a small canonical set."""
    t = (t or "").strip()
    if not t:
        return ""
    tl = t.lower()

    if "hotel" in tl or re.search(r"(숙소|호텔)", t):
        return "Hotel"
    if any(k in tl for k in ["restaurant", "food", "dining", "eatery"]) or re.search(r"(맛집|식당|레스토랑)", t):
        return "Restaurant"
    if any(k in tl for k in ["cafe", "coffee", "bakery"]) or re.search(r"(카페|베이커리)", t):
        return "Cafe"
    if any(k in tl for k in ["attraction", "landmark", "museum", "temple", "church", "palace", "park", "zoo", "aquarium", "tower", "market"]):
        return "Attraction"
    if re.search(r"(관광|명소|역사|문화|박물관|사원|성당|궁|공원|동물원|수족관|타워|시장|광장|산|자연)", t):
        return "Attraction"
    return t


def _emoji_for_type(t: str) -> str:
    t = _canonical_type(t)
    if t == "Hotel":
        return "🏨"
    if t == "Restaurant":
        return "🍽️"
    if t == "Cafe":
        return "☕"
    if t == "Attraction":
        return "🗺️"
    return "📍"


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    R = 6371.0
    phi1 = math.radians(float(lat1)); phi2 = math.radians(float(lat2))
    dphi = math.radians(float(lat2) - float(lat1))
    dl = math.radians(float(lon2) - float(lon1))
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dl/2)**2
    return 2*R*math.asin(math.sqrt(a))


def _route_stats(points: List[Dict[str, Any]]) -> Dict[str, Any]:
    segs = []
    total_km = 0.0
    for i in range(len(points)-1):
        a, b = points[i], points[i+1]
        if a.get("lat") is None or a.get("lon") is None or b.get("lat") is None or b.get("lon") is None:
            continue
        d = _haversine_km(a["lat"], a["lon"], b["lat"], b["lon"])
        total_km += d
        segs.append({"from": a.get("name"), "to": b.get("name"), "km": round(d, 2)})
    return {"total_km": round(total_km, 2), "segments": segs}


def build_day_context(*, city: str, country: str, day_number: int, hotel: Optional[Dict[str, Any]], pois: List[Dict[str, Any]]) -> Dict[str, Any]:
    points: List[Dict[str, Any]] = []

    # hotel (start/end)
    if hotel and hotel.get("selected") and hotel["selected"].get("lat") and hotel["selected"].get("lon"):
        hs = hotel["selected"]
        points.append({
            "name": f"숙소: {hs.get('name','')}".strip(),
            "type": "Hotel",
            "lat": hs.get("lat"),
            "lon": hs.get("lon"),
        })

    for p in pois:
        points.append({
            "name": p.get("name"),
            "type": _canonical_type(p.get("type") or p.get("category")),
            "lat": p.get("lat"),
            "lon": p.get("lon"),
        })

    # return-to-hotel
    if len(points) >= 2 and _canonical_type(points[0].get("type")) == "Hotel":
        points.append(points[0])

    stats = _route_stats(points)
    return {
        "city": city,
        "country": country,
        "day_number": day_number,
        "points": points,
        "stats": stats,
    }


def _build_route_header(points: List[Dict[str, Any]]) -> str:
    # Typed route line (always present)
    chunks = []
    names_by_type = {"Restaurant": [], "Cafe": [], "Attraction": []}

    for p in points:
        name = (p.get("name") or "").strip()
        if not name:
            continue
        t = _canonical_type(p.get("type"))
        chunks.append(f"{_emoji_for_type(t)} {name}")
        if t in names_by_type and len(names_by_type[t]) < 3:
            # store short list for quick glance
            names_by_type[t].append(name.replace("숙소:", "").strip())

    header_lines = []
    if chunks:
        header_lines.append("📍 오늘 일정(순서)")
        header_lines.append(" → ".join(chunks))

        # quick checklist so "맛집"이 절대 누락되지 않도록
        quick = []
        if names_by_type["Restaurant"]:
            quick.append("🍽️ 맛집: " + ", ".join(names_by_type["Restaurant"]))
        if names_by_type["Cafe"]:
            quick.append("☕ 카페: " + ", ".join(names_by_type["Cafe"]))
        if names_by_type["Attraction"]:
            quick.append("🗺️ 관광: " + ", ".join(names_by_type["Attraction"]))
        if quick:
            header_lines.append("")
            header_lines.extend(quick)

        header_lines.append("")
    return "\n".join(header_lines)


def generate_day_explanation(day_ctx: Dict[str, Any], *, top_k: int = 5) -> Dict[str, Any]:
    city = day_ctx.get("city", "")
    country = day_ctx.get("country", "")
    points = day_ctx.get("points", [])
    stats = day_ctx.get("stats", {})

    header = _build_route_header(points)
    total_km = stats.get("total_km", 0)

    poi_names = [p.get("name", "") for p in points if _canonical_type(p.get("type")) != "Hotel"]

    # Add English keywords as well so retrieval works for English corpora (e.g., Tokyo.md)
    query = (
        f"{city} 동선 이동 팁 교통 주의사항 예약 혼잡 "
        f"route optimize transit metro train reservation crowd safety "
        + " ".join(poi_names[:10])
    )

    rag_hits = rag_search(query=query, city=city, top_k=top_k)

    sources_block = ""
    for i, h in enumerate(rag_hits, start=1):
        sources_block += f"[{i}] {h.get('title')} — {h.get('snippet')}\n"

    route_line_plain = " → ".join([p.get("name", "") for p in points if p.get("name")])

    sys = (
        "너는 10년 경력의 현지 전문 여행 가이드이자 일정 컨설턴트야.\n"
        "말투는 친절하지만 단정한 전문가 톤(한국어)으로, 과장/추측은 금지해.\n"
        "반드시 제공된 일정 정보와 '출처(Sources)'에만 근거해서 설명해.\n"
        "동선/거리(계산 가능한 정보)는 인용 없이 설명 가능하되, 규정/정책/교통 규칙 같은 사실은 Sources가 없으면 단정하지 마.\n"
        "출처가 필요한 문장은 [1] [2] 처럼 번호로 인용해.\n"
        "중요: 일정에 맛집/카페/관광이 포함되어 있으면 각각을 최소 1번은 언급해.\n"
    )

    user = (
        f"도시: {country} / {city}\n"
        f"Day {day_ctx.get('day_number')}\n"
        f"일정(순서): {route_line_plain}\n"
        f"추정 총 이동거리(직선): 약 {total_km} km\n\n"
        "요청:\n"
        "- 왜 이 순서가 이동에 유리한지 5~8줄로 설명해줘.\n"
        "- 가능하면 '관광→식사→카페→관광'처럼 유두리 있게 짚어줘.\n"
        "- 마지막에 실전 팁 3개를 bullet로 써줘(있으면 출처 인용).\n\n"
        "Sources:\n"
        f"{sources_block if sources_block else '(no sources)'}\n"
    )

    used_llm = False
    text = None
    if is_llm_configured():
        text = generate_explanation_openai(system=sys, user=user)
        if text:
            used_llm = True

    if not text:
        # Deterministic fallback (include route header already)
        lines = []
        lines.append(f"오늘 일정은 **이동 동선이 매끄럽게 이어지도록** 가까운 장소 위주로 구성했습니다(직선 기준 약 {total_km}km).")
        if points and _canonical_type(points[0].get("type")) == "Hotel":
            lines.append("숙소에서 출발해 같은 권역을 중심으로 움직인 뒤, 마지막에 숙소로 자연스럽게 복귀하는 흐름입니다.")

        types = [_canonical_type(p.get("type")) for p in points if _canonical_type(p.get("type")) and _canonical_type(p.get("type")) != "Hotel"]
        if "Restaurant" in types and "Cafe" in types and "Attraction" in types:
            lines.append("관광(명소) 사이에 식사/카페를 배치해 체력 안배가 되도록 구성했습니다.")
        if stats.get("segments"):
            s0 = stats["segments"][:3]
            seg_txt = ", ".join([f"{s['from']}→{s['to']} {s['km']}km" for s in s0 if s.get("from") and s.get("to")])
            if seg_txt:
                lines.append(f"초반 이동 예시: {seg_txt}.")
        lines.append("")
        lines.append("가이드 팁:")
        lines.append("• 인기 맛집/카페는 피크 타임(12~13시, 18~19시)을 살짝 피하면 대기 시간을 줄일 수 있어요.")
        lines.append("• 일정이 바뀌면 ‘일정 설명해줘’라고 말하면 최신 동선 기준으로 다시 정리해드릴게요.")
        lines.append("• 대중교통/도보 중심이면 교통카드·환승 규칙을 먼저 확인해두면 좋아요(출처가 있으면 인용).")
        text = "\n".join(lines)

    final_text = (header + (text.strip() if text else "")).strip()
    return {
        "text": final_text,
        "sources": rag_hits,
        "used_llm": used_llm,
    }
