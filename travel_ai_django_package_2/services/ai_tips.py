"""AI Tips generator (LLM + RAG with safe fallback).

Goal
----
Produce short, actionable travel tips for a given day itinerary.

Requirements
------------
- Prefer grounding in retrieved sources when possible.
- Use inline citations like [1], [2] that correspond to the retrieval hits.
- If no LLM key is configured, still return usable tips (RAG-only deterministic fallback).
"""

from __future__ import annotations

import re
from typing import Dict, Any, List, Optional

from .rag_engine import search as rag_search
from .llm_client import generate_explanation_openai, is_llm_configured


_BULLET_RE = re.compile(r"^\s*([\-\*•]+)\s+")


def _normalize_bullet(line: str) -> str:
    line = (line or "").strip()
    if not line:
        return ""
    line = _BULLET_RE.sub("", line).strip()
    # unified bullet marker
    return f"• {line}" if line else ""


def _has_citation(s: str) -> bool:
    return bool(re.search(r"\[[0-9]+\]", s or ""))


def _first_sentence(text: str) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    if not t:
        return ""
    # split by Korean/English sentence terminators
    m = re.split(r"(?<=[\.!?。])\s+|(?<=\.)\s+|(?<=\?)\s+|(?<=!)\s+|(?<=요\.)\s+", t, maxsplit=1)
    if m:
        return m[0].strip()
    return t


def generate_day_tips(
    day_ctx: Dict[str, Any],
    *,
    top_k: int = 5,
    max_bullets: int = 3,
) -> Dict[str, Any]:
    """Return tips payload.

    Returns dict:
      {
        "bullets": ["• ... [1]", ...],
        "sources": [{rank, title, snippet, ...}, ...],
        "used_llm": bool,
      }
    """

    city = (day_ctx.get("city") or "").strip()
    country = (day_ctx.get("country") or "").strip()
    points = day_ctx.get("points") or []

    poi_names = [p.get("name", "") for p in points if p.get("type") != "Hotel" and p.get("name")]
    # Add some English keywords to work well with English corpora (e.g., Tokyo.md)
    query = (
        f"{city} 여행 팁 교통 주의사항 예약 혼잡 소매치기 현금 카드 "
        f"travel tips transit metro train reservation crowd safety cash card "
        + " ".join(poi_names[:8])
    )

    rag_hits = rag_search(query=query, city=city, top_k=top_k)

    # Build a prompt with sources
    sources_block = ""
    for i, h in enumerate(rag_hits, start=1):
        sources_block += f"[{i}] {h.get('title')} — {h.get('snippet')}\n"

    sys = (
        "너는 10년차 현지 전문 여행 가이드야.\n"
        "말투는 친절하지만 단정한 전문가 톤(한국어)으로.\n"
        "반드시 제공된 Sources에 근거해서만 말하고, 추측/과장은 금지해.\n"
        "각 팁은 짧고 실행 가능하게 1문장으로, 끝에는 반드시 [1] [2] 같은 출처 번호를 붙여.\n"
    )

    user = (
        f"도시: {country} / {city}\n"
        f"방문 후보: {', '.join(poi_names[:8])}\n\n"
        "요청:\n"
        f"- 아래 Sources를 바탕으로 오늘 일정에 도움이 되는 팁 {max_bullets}개를 bullet로 작성해줘.\n"
        "- 이동/교통, 예약/혼잡, 안전/유의사항 중심.\n"
        "- 각 bullet 끝에는 반드시 출처 번호를 붙여([1] 처럼).\n\n"
        "Sources:\n"
        f"{sources_block if sources_block else '(no sources)'}\n"
    )

    used_llm = False
    bullets: List[str] = []

    # 1) LLM path (optional)
    if is_llm_configured() and rag_hits:
        text = generate_explanation_openai(system=sys, user=user, max_tokens=260, temperature=0.2)
        if text:
            used_llm = True
            for line in (text or "").splitlines():
                b = _normalize_bullet(line)
                if not b:
                    continue
                bullets.append(b)
            # keep only desired count
            bullets = [b for b in bullets if b.startswith("• ")]

    # 2) Deterministic fallback (RAG-only)
    if len(bullets) < max_bullets:
        # Create tips from top snippets (first sentence)
        for i, h in enumerate(rag_hits[:max(1, max_bullets)], start=1):
            sent = _first_sentence(h.get("snippet") or "")
            if not sent:
                continue
            b = f"• {sent} [{i}]"
            bullets.append(b)

    # 3) Final normalize: ensure citations exist (only when sources exist), keep readable length
    clean: List[str] = []
    for i, b in enumerate(bullets):
        bb = _normalize_bullet(b)
        if not bb:
            continue
        # Add a default citation if missing (only when we have sources)
        if rag_hits and not _has_citation(bb):
            bb = (bb + f" [{min(i+1, len(rag_hits))}]").strip()
        # Keep it within a reasonable length; PDF will do additional fitting.
        if len(bb) > 160:
            bb = bb[:158].rstrip() + "…"
        if bb not in clean:
            clean.append(bb)
        if len(clean) >= max_bullets:
            break

    # 4) If no sources and no bullets, provide safe generic tips
    if not clean:
        clean = [
            "• 대중교통 비중이 크다면 교통카드/패스를 미리 준비해 이동 시간을 줄이세요.",
            "• 인기 스폿은 피크 타임(점심/저녁)을 30~60분만 피하셔도 대기 시간이 크게 줄어듭니다.",
            "• 현금만 받는 곳이 드물게 있으니, 소액 현금 + 카드/간편결제를 함께 준비해두세요.",
        ]

    return {
        "bullets": clean[:max_bullets],
        "sources": rag_hits,
        "used_llm": bool(used_llm),
    }
