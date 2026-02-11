"""
Day itinerary explanation generator (LLM + RAG with safe fallback).
- Uses services.rag_engine.search for retrieval
- Uses services.llm_client.generate_explanation_openai for LLM (optional)
- Always returns a usable explanation text.
"""
from __future__ import annotations

import math
import re
from typing import Dict, Any, List, Optional, Tuple

from .rag_engine import search as rag_search
from .llm_client import generate_explanation_openai, is_llm_configured


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


def build_day_context(*, city: str, country: str, day_number: int, hotel: Optional[Dict[str,Any]], pois: List[Dict[str,Any]]) -> Dict[str,Any]:
    points: List[Dict[str,Any]] = []
    if hotel and hotel.get("selected") and hotel["selected"].get("lat") and hotel["selected"].get("lon"):
        hs = hotel["selected"]
        points.append({"name": f"숙소: {hs.get('name','')}".strip(), "type": "Hotel", "lat": hs.get("lat"), "lon": hs.get("lon")})
    for p in pois:
        points.append({"name": p.get("name"), "type": p.get("type") or p.get("category"), "lat": p.get("lat"), "lon": p.get("lon")})
    # return-to-hotel
    if len(points) >= 2 and points[0].get("type") == "Hotel":
        points.append(points[0])

    stats = _route_stats(points)

    return {
        "city": city,
        "country": country,
        "day_number": day_number,
        "points": points,
        "stats": stats,
    }


def generate_day_explanation(day_ctx: Dict[str,Any], *, top_k: int = 5) -> Dict[str,Any]:
    city = day_ctx.get("city","")
    country = day_ctx.get("country","")
    points = day_ctx.get("points",[])
    stats = day_ctx.get("stats",{})

    poi_names = [p.get("name","") for p in points if p.get("type") != "Hotel"]
    query = f"{city} 동선 이동 팁 교통 주의사항 예약 혼잡 " + " ".join(poi_names[:8])

    rag_hits = rag_search(query=query, city=city, top_k=top_k)

    # Build a safe LLM prompt
    sources_block = ""
    for i, h in enumerate(rag_hits, start=1):
        sources_block += f"[{i}] {h.get('title')} — {h.get('snippet')}\n"

    route_line = " → ".join([p.get("name","") for p in points if p.get("name")])
    total_km = stats.get("total_km", 0)

    sys = (
        "너는 여행 일정 설명을 도와주는 어시스턴트야.\n"
        "반드시 제공된 일정 정보와 '출처(Sources)'에만 근거해서 설명해.\n"
        "모르는 내용은 추측하지 말고, 일정 최적화/동선 관점에서 말해.\n"
        "출처가 필요한 문장은 [1] [2] 처럼 번호로 인용해.\n"
        "출처가 없는 경우(동선/거리 계산)는 인용 없이 가능.\n"
    )

    user = (
        f"도시: {country} / {city}\n"
        f"Day {day_ctx.get('day_number')}\n"
        f"일정(순서): {route_line}\n"
        f"추정 총 이동거리(직선): 약 {total_km} km\n\n"
        "요청:\n"
        "- 왜 이 순서가 이동에 유리한지 5~8줄로 설명해줘.\n"
        "- 가능하면 '관광→식사→카페→관광'처럼 유두리 있게 짚어줘.\n"
        "- 마지막에 실전 팁 3개를 bullet로 써줘(있으면 출처 인용).\n\n"
        "Sources:\n"
        f"{sources_block if sources_block else '(no sources)'}\n"
    )

    text = None
    if is_llm_configured():
        text = generate_explanation_openai(system=sys, user=user)

    if not text:
        # Deterministic fallback
        lines = []
        lines.append(f"오늘 일정은 이동거리가 짧게 이어지도록 가까운 장소 위주로 배열되었습니다(추정 {total_km}km).")
        if points and points[0].get("type") == "Hotel":
            lines.append("숙소에서 출발해 주변 구역을 크게 한 바퀴 돌고, 마지막에 숙소로 돌아오는 흐름입니다.")
        # Pattern hint
        types = [p.get("type") for p in points if p.get("type") and p.get("type") != "Hotel"]
        if "Restaurant" in types and "Cafe" in types and ("Attraction" in types or "Landmark" in types):
            lines.append("관광(명소) 사이에 식사/카페를 배치해 체력 안배가 되도록 구성했습니다.")
        if day_ctx.get("stats", {}).get("segments"):
            s0 = day_ctx["stats"]["segments"][:3]
            seg_txt = ", ".join([f"{s['from']}→{s['to']} {s['km']}km" for s in s0 if s.get("from") and s.get("to")])
            if seg_txt:
                lines.append(f"초반 이동 예시: {seg_txt}.")
        lines.append("")
        lines.append("실전 팁:")
        lines.append("• 이동이 길어 보이면 ‘🚀 이동 최적화’ 버튼으로 다시 정렬해보세요.")
        lines.append("• 인기 맛집/카페는 피크 타임(12~13시, 18~19시)을 살짝 피하면 대기 시간을 줄일 수 있어요.")
        lines.append("• 일정이 바뀌면 ‘설명 다시 생성’으로 최신 동선에 맞게 업데이트하세요.")
        text = "\n".join(lines)

    return {
        "text": text.strip(),
        "sources": rag_hits,
        "used_llm": bool(is_llm_configured() and text),
    }
