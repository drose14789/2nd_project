"""
Lightweight RAG engine (no external deps).
- Loads markdown docs from <BASE_DIR>/rag/corpus/*.md
- Splits into chunks and ranks with a simple BM25 implementation.
"""
from __future__ import annotations

import os
import re
import math
from dataclasses import dataclass
from typing import List, Dict, Any, Optional, Tuple
from django.conf import settings


_WORD_RE = re.compile(r"[0-9A-Za-z가-힣]+", re.UNICODE)

# City aliases to improve retrieval when UI city names are localized (e.g., "도쿄")
# while corpus docs are in English (e.g., "Tokyo.md").
_CITY_ALIASES = {
    # Korea
    "서울": ["Seoul"],
    "부산": ["Busan"],
    "제주": ["Jeju"],
    # Japan
    "도쿄": ["Tokyo"],
    "오사카": ["Osaka"],
    "교토": ["Kyoto"],
    "삿포로": ["Sapporo"],
    "후쿠오카": ["Fukuoka"],
    "나고야": ["Nagoya"],
    # SEA / etc (extend as needed)
    "방콕": ["Bangkok"],
    "호치민": ["Ho Chi Minh", "HCMC", "Saigon"],
    "다낭": ["Da Nang", "Danang"],
    # Europe
    "파리": ["Paris"],
    "런던": ["London"],
    "로마": ["Rome"],
    "피렌체": ["Florence"],
    "비엔나": ["Vienna"],
}


def _city_aliases(city: str) -> List[str]:
    city = (city or "").strip()
    if not city:
        return []
    aliases = [city]
    aliases.extend(_CITY_ALIASES.get(city, []))
    # Also include an underscore/slug-ish variant sometimes used in filenames
    slug = re.sub(r"\s+", "_", city).strip("_")
    if slug and slug != city:
        aliases.append(slug)
    # de-dup
    out = []
    seen = set()
    for a in aliases:
        k = re.sub(r"\s+", "", a).lower()
        if k and k not in seen:
            seen.add(k)
            out.append(a)
    return out


def _tokenize(text: str) -> List[str]:
    return [t.lower() for t in _WORD_RE.findall(text or "") if t.strip()]


def _split_chunks(text: str, max_chars: int = 650, overlap: int = 80) -> List[str]:
    text = (text or "").strip()
    if not text:
        return []
    # Prefer paragraph split
    paras = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    chunks: List[str] = []
    buf = ""
    for p in paras:
        if len(buf) + len(p) + 2 <= max_chars:
            buf = (buf + "\n\n" + p).strip() if buf else p
        else:
            if buf:
                chunks.append(buf.strip())
            # If a single paragraph is too long, hard-split
            if len(p) > max_chars:
                start = 0
                while start < len(p):
                    end = min(len(p), start + max_chars)
                    chunks.append(p[start:end].strip())
                    start = max(0, end - overlap)
                buf = ""
            else:
                buf = p
    if buf:
        chunks.append(buf.strip())
    # Add overlap between chunks (soft) by appending tail from previous
    out: List[str] = []
    prev_tail = ""
    for c in chunks:
        if prev_tail:
            out.append((prev_tail + "\n" + c).strip())
        else:
            out.append(c)
        prev_tail = c[-overlap:] if len(c) > overlap else c
    return out


@dataclass
class _Chunk:
    doc_id: str
    title: str
    path: str
    text: str
    tokens: List[str]


class _BM25:
    def __init__(self, docs_tokens: List[List[str]], k1: float = 1.2, b: float = 0.75):
        self.docs_tokens = docs_tokens
        self.k1 = k1
        self.b = b
        self.N = len(docs_tokens)
        self.avgdl = sum(len(t) for t in docs_tokens) / (self.N or 1)

        self.df: Dict[str, int] = {}
        for toks in docs_tokens:
            seen = set(toks)
            for t in seen:
                self.df[t] = self.df.get(t, 0) + 1

        # Precompute idf
        self.idf: Dict[str, float] = {}
        for t, df in self.df.items():
            # classic BM25 idf
            self.idf[t] = math.log(1 + (self.N - df + 0.5) / (df + 0.5))

    def score(self, query_tokens: List[str], doc_tokens: List[str]) -> float:
        if not doc_tokens:
            return 0.0
        freqs: Dict[str, int] = {}
        for t in doc_tokens:
            freqs[t] = freqs.get(t, 0) + 1
        dl = len(doc_tokens)
        score = 0.0
        for t in query_tokens:
            if t not in freqs:
                continue
            idf = self.idf.get(t, 0.0)
            tf = freqs[t]
            denom = tf + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1))
            score += idf * (tf * (self.k1 + 1) / (denom or 1))
        return float(score)


_CACHE: Dict[str, Any] = {"chunks": None, "bm25": None, "loaded_from": None}


def _load_corpus() -> Tuple[List[_Chunk], _BM25]:
    # Cache in-process
    if _CACHE["chunks"] is not None and _CACHE["bm25"] is not None:
        return _CACHE["chunks"], _CACHE["bm25"]

    base_dir = getattr(settings, "BASE_DIR", os.getcwd())
    corpus_dir = os.path.join(base_dir, "rag", "corpus")
    chunks: List[_Chunk] = []

    if os.path.isdir(corpus_dir):
        for fn in sorted(os.listdir(corpus_dir)):
            if not fn.lower().endswith(".md"):
                continue
            path = os.path.join(corpus_dir, fn)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    md = f.read()
            except Exception:
                continue

            # Title from first markdown header
            title = fn[:-3]
            m = re.search(r"^#\s+(.+)$", md, re.MULTILINE)
            if m:
                title = m.group(1).strip()

            doc_id = fn[:-3].strip()
            for ch in _split_chunks(md):
                chunks.append(_Chunk(
                    doc_id=doc_id,
                    title=title,
                    path=path,
                    text=ch,
                    tokens=_tokenize(ch),
                ))

    bm25 = _BM25([c.tokens for c in chunks]) if chunks else _BM25([])
    _CACHE["chunks"] = chunks
    _CACHE["bm25"] = bm25
    _CACHE["loaded_from"] = corpus_dir
    return chunks, bm25


def search(query: str, city: Optional[str] = None, top_k: int = 5) -> List[Dict[str, Any]]:
    """
    Returns: list of {rank, doc_id, title, snippet}
    """
    chunks, bm25 = _load_corpus()
    if not chunks:
        return []

    q = (query or "").strip()
    aliases: List[str] = []
    if city:
        aliases = _city_aliases(city)
        # Include aliases at the front so BM25 sees them as strong query terms.
        q = (" ".join(aliases) + " " + q).strip()

    q_tokens = _tokenize(q)
    if not q_tokens:
        return []

    scored: List[Tuple[float, int]] = []
    for i, c in enumerate(chunks):
        # small boost if doc_id/title matches city or any alias (loose)
        boost = 0.0
        if aliases:
            did = re.sub(r"\s+", "", c.doc_id).lower()
            ttl = re.sub(r"\s+", "", c.title).lower()
            for a in aliases:
                ckey = re.sub(r"\s+", "", a).lower()
                if ckey and (ckey in did or ckey in ttl):
                    boost = 0.6
                    break
        s = bm25.score(q_tokens, c.tokens) + boost
        if s > 0:
            scored.append((s, i))

    # Fallback: if city-scoped query yields nothing, retry without relying on city terms.
    if not scored and city:
        q2 = (query or "").strip()
        # still include aliases once (helps match doc filename/title)
        if aliases:
            q2 = (" ".join(aliases) + " " + q2).strip()
        q2_tokens = _tokenize(q2)
        if q2_tokens:
            for i, c in enumerate(chunks):
                s = bm25.score(q2_tokens, c.tokens)
                if s > 0:
                    scored.append((s, i))
            scored.sort(key=lambda x: x[0], reverse=True)

    scored.sort(key=lambda x: x[0], reverse=True)
    out: List[Dict[str, Any]] = []
    for rank, (s, idx) in enumerate(scored[:max(1, top_k)], start=1):
        c = chunks[idx]
        snippet = c.text.strip()
        # shorten snippet
        snippet = re.sub(r"\s+", " ", snippet)
        if len(snippet) > 260:
            snippet = snippet[:260].rstrip() + "…"
        out.append({
            "rank": rank,
            "score": round(float(s), 4),
            "doc_id": c.doc_id,
            "title": c.title,
            "snippet": snippet,
        })
    return out
