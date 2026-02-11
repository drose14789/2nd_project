"""
services/llm.py - LLM 호출 서비스 (OpenAI / Anthropic 자동 분기)

사용법:
    from services.llm import ask_with_rag, call_llm

    # RAG + LLM 통합 (가장 자주 쓸 함수)
    answer = ask_with_rag("도쿄에서 꼭 가봐야 할 곳은?", city="tokyo")

    # LLM 직접 호출
    answer = call_llm("안녕하세요", model="gpt-4o-mini")
"""
import json
import logging
from typing import List, Tuple, Optional, Dict, Any

from django.conf import settings

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  LLM Provider 추상화
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def get_default_model() -> str:
    return getattr(settings, "LLM_MODEL", "gpt-4o-mini")


def call_llm(
    prompt: str,
    system_prompt: str = "",
    model: str = None,
    temperature: float = 0.7,
    max_tokens: int = 2000,
    response_format: str = "text",  # "text" or "json"
) -> str:
    """
    LLM API 호출. model 이름에 따라 OpenAI / Anthropic 자동 분기.
    """
    model = model or get_default_model()
    provider = "anthropic" if "claude" in model else "openai"

    try:
        if provider == "openai":
            return _call_openai(prompt, system_prompt, model, temperature, max_tokens, response_format)
        else:
            return _call_anthropic(prompt, system_prompt, model, temperature, max_tokens)
    except Exception as e:
        logger.error(f"LLM call failed ({provider}/{model}): {e}")
        return ""


def _call_openai(prompt, system_prompt, model, temperature, max_tokens, response_format):
    from openai import OpenAI

    client = OpenAI(api_key=settings.OPENAI_API_KEY)

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    kwargs = dict(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    if response_format == "json":
        kwargs["response_format"] = {"type": "json_object"}

    response = client.chat.completions.create(**kwargs)
    return response.choices[0].message.content or ""


def _call_anthropic(prompt, system_prompt, model, temperature, max_tokens):
    import anthropic

    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

    kwargs = dict(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        messages=[{"role": "user", "content": prompt}],
    )
    if system_prompt:
        kwargs["system"] = system_prompt

    response = client.messages.create(**kwargs)
    return response.content[0].text or ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  RAG + LLM 통합 함수
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def ask_with_rag(
    query: str,
    city: str = None,
    country: str = None,
    topic: str = None,
    system_prompt: str = "",
    model: str = None,
    top_k: int = 5,
    retrieve_k: int = None,
    final_k: int = None,
    return_sources: bool = False,
    routed: Dict[str, Any] = None,
) -> Any:
    """
    RAG 검색 → 컨텍스트 주입 → LLM 응답.

    - 하위호환: ask_with_rag(query, city="tokyo") -> str
    - 확장: country/city/topic + retrieve_k/final_k + sources 반환

    return_sources=True면 {"answer": str, "sources": [...], "routed": {...}} 반환
    """
    from rag.engine import rag_search

    final_k = final_k or top_k

    # 1) RAG 검색
    chunks = rag_search(
        query=query,
        city=city,
        country=country,
        topic=topic,
        retrieve_k=retrieve_k,
        final_k=final_k,
        top_k=top_k,
    )

    # 2) 컨텍스트 구성 (+ sources)
    sources = []
    context_parts = []
    for i, c in enumerate(chunks, start=1):
        sid = f"S{i}"
        snippet = (c.get("text") or "").strip()
        sources.append({
            "id": sid,
            "source": c.get("source"),
            "path": c.get("path"),
            "country": c.get("country"),
            "city": c.get("city"),
            "topic": c.get("topic"),
            "score": c.get("score"),
            "snippet": snippet[:400],  # 너무 길면 잘라서 내려줌
        })
        context_parts.append(f"[{sid} | {c.get('source')}] {snippet}")

    context = "\\n\\n---\\n\\n".join(context_parts) if context_parts else "(관련 참고 자료 없음)"

    # 3) 프롬프트 조합 (근거 인용 강제)
    full_prompt = f"""아래 참고 자료를 바탕으로 질문에 답변하세요.

규칙:
- 참고 자료에 근거한 내용은 반드시 [S#] 형태로 인용하세요.
- 참고 자료에 없는 내용을 단정하지 마세요. 필요하면 '추가 정보가 필요합니다'라고 말하세요.
- 답변은 한국어로, 실용적으로 작성하세요.

## 참고 자료
{context}

## 질문
{query}
"""

    if not system_prompt:
        system_prompt = (
            "당신은 전문 여행 플래너이자 여행 Q&A 챗봇입니다. "
            "과장 없이, 근거 기반으로 답변합니다. "
            "필요하면 추가 질문으로 요구사항을 좁히세요."
        )

    answer = call_llm(
        prompt=full_prompt,
        system_prompt=system_prompt,
        model=model,
    )

    if not return_sources:
        return answer

    return {
        "answer": answer,
        "sources": sources,
        "routed": routed or {"country": country, "city": city, "topic": topic},
    }


def generate_itinerary_with_llm(
    city: str,
    country: str,
    days: int,
    style: str = "mixed",
    preferences: Dict[str, Any] = None,
    poi_candidates: List[dict] = None,
    model: str = None,
) -> List[dict]:
    """
    LLM으로 일정 생성/최적화.

    Returns: [{"day": 1, "theme": "...", "pois": [...], "tip": "..."}, ...]
    """
    from rag.engine import rag_search

    rag_results = rag_search(
        f"{city} 여행 추천 일정 관광지 맛집", city=city, top_k=5
    )
    context = "\n".join([text for text, _, _ in rag_results])

    poi_info = ""
    if poi_candidates:
        poi_names = [p.get("name", "") for p in poi_candidates[:20]]
        poi_info = f"\n\n사용 가능한 장소: {', '.join(poi_names)}"

    pref_info = ""
    if preferences:
        pref_info = f"\n여행자 선호: 스타일={style}, 상세={json.dumps(preferences, ensure_ascii=False)}"

    system_prompt = """당신은 전문 여행 플래너입니다. 반드시 JSON 형식으로만 응답하세요.
응답 형식:
{
  "days": [
    {
      "day": 1,
      "theme": "역사와 문화",
      "pois": [
        {
          "name": "장소명",
          "category": "landmark|restaurant|cafe|shopping",
          "time_slot": "morning|afternoon|evening",
          "duration_min": 90,
          "reason": "추천 이유 (1줄)"
        }
      ],
      "tip": "이 날의 실용 팁"
    }
  ]
}"""

    prompt = f"""{city}({country}) {days}일 여행 일정을 만들어주세요.
{pref_info}

## 도시 정보
{context}
{poi_info}

하루에 4~5개 장소를 포함하고, 동선이 효율적이도록 배치하세요.
JSON으로 응답하세요."""

    response = call_llm(
        prompt=prompt,
        system_prompt=system_prompt,
        model=model,
        response_format="json",
        max_tokens=3000,
    )

    try:
        # JSON 파싱 (```json 래퍼 제거)
        cleaned = response.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1]
        if cleaned.endswith("```"):
            cleaned = cleaned.rsplit("```", 1)[0]
        data = json.loads(cleaned)
        return data.get("days", [])
    except (json.JSONDecodeError, AttributeError) as e:
        logger.error(f"LLM JSON parse error: {e} | response: {response[:300]}")
        return []


def generate_travel_tip_llm(
    city: str,
    day_number: int,
    pois: List[str] = None,
    model: str = None,
) -> str:
    """LLM으로 맞춤 여행 팁 1~2줄 생성"""
    from rag.engine import rag_search

    rag_results = rag_search(f"{city} 여행 팁 주의사항", city=city, top_k=3)
    context = "\n".join([text for text, _, _ in rag_results])

    poi_info = f"오늘 방문 장소: {', '.join(pois)}" if pois else ""

    prompt = f"""{city} 여행 {day_number}일차에 유용한 실용 팁 1~2개를 간결하게 알려주세요.
{poi_info}

참고 정보:
{context}

50자 이내의 팁만 간결하게 답변하세요. 번호 없이, 팁만 작성하세요."""

    return call_llm(prompt=prompt, model=model, max_tokens=200, temperature=0.8)

