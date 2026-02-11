"""
LLM client wrapper (OpenAI).
- Safe: if API key missing or library unavailable, returns None.
"""
from __future__ import annotations

import os
from typing import Optional, List, Dict, Any, Tuple

def is_llm_configured() -> bool:
    return bool(os.getenv("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY".lower()))

def generate_explanation_openai(
    *,
    system: str,
    user: str,
    model: Optional[str] = None,
    temperature: float = 0.3,
    max_tokens: int = 650,
) -> Optional[str]:
    """
    Returns the assistant text, or None on failure.
    """
    api_key = os.getenv("OPENAI_API_KEY") or os.getenv("openai_api_key")
    if not api_key:
        return None

    try:
        # OpenAI python v1+
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        mdl = model or os.getenv("OPENAI_MODEL") or "gpt-4o-mini"
        resp = client.chat.completions.create(
            model=mdl,
            temperature=temperature,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return resp.choices[0].message.content
    except Exception:
        return None
