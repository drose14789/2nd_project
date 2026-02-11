"""
rag/engine.py - 벡터 기반 RAG 검색 엔진 (FAISS + OpenAI Embeddings)

✅ 확장 포인트(25개 도시 이상 대응)
- corpus 폴더를 재귀적으로 탐색: rag/corpus/<country>/<city>/<topic>.md
- chunk size/overlap, retrieve_k/final_k를 .env로 제어
- country/city/topic 메타데이터 필터
- (선택) reranker로 최종 문맥 정밀도 향상
"""
from __future__ import annotations

import os
import glob
import json
import hashlib
import logging
import numpy as np
from pathlib import Path
from typing import List, Tuple, Optional, Dict, Any

logger = logging.getLogger(__name__)

# ── 설정 ──────────────────────────────────────
THIS_DIR = Path(__file__).resolve().parent
CORPUS_DIR = str(THIS_DIR / "corpus")
_DEFAULT_INDEX_DIR = str(THIS_DIR / "index")


def _get_safe_index_dir() -> str:
    """FAISS는 Windows에서 non-ASCII 경로를 못 씀 → 안전한 경로 반환"""
    import sys
    try:
        _DEFAULT_INDEX_DIR.encode("ascii")
        return _DEFAULT_INDEX_DIR
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    if sys.platform == "win32":
        return os.path.join(os.path.expanduser("~"), ".travel_ai_rag_index")
    import tempfile
    return os.path.join(tempfile.gettempdir(), "travel_ai_rag_index")


INDEX_DIR = _get_safe_index_dir()

# env 기반(기본값은 기존값보다 작게 = 정확도↑)
CHUNK_SIZE_WORDS = int(os.getenv("RAG_CHUNK_WORDS", "250"))
CHUNK_OVERLAP_WORDS = int(os.getenv("RAG_CHUNK_OVERLAP_WORDS", "50"))

RAG_RETRIEVE_K = int(os.getenv("RAG_RETRIEVE_K", "40"))  # 넓게 뽑기
RAG_FINAL_K = int(os.getenv("RAG_FINAL_K", "8"))         # LLM에 넣는 최종 개수

TOP_K = int(os.getenv("RAG_TOP_K", "5"))  # 하위호환(예전 코드)
EMBEDDING_MODEL = os.getenv("RAG_EMBEDDING_MODEL", "text-embedding-3-small")

ENABLE_RERANK = os.getenv("RAG_ENABLE_RERANK", "0").strip() in ("1", "true", "True")
RERANK_MODEL = os.getenv("RAG_RERANK_MODEL", "bge-reranker-v2-m3").strip()


def _parse_meta_from_path(md_path: str) -> Dict[str, Optional[str]]:
    """
    md_path 예:
      .../rag/corpus/jp/tokyo/transport.md  -> country=jp, city=tokyo, topic=transport
      .../rag/corpus/Tokyo.md              -> city=tokyo (구버전 호환)
    """
    p = Path(md_path)
    stem = p.stem
    parts = p.as_posix().split("/")
    meta: Dict[str, Optional[str]] = {"country": None, "city": None, "topic": None, "source": stem}

    # corpus/<country>/<city>/<topic>.md
    try:
        idx = parts.index("corpus")
        # 최소 corpus/x/y/z.md
        if len(parts) - idx >= 4:
            meta["country"] = parts[idx + 1].lower()
            meta["city"] = parts[idx + 2].lower()
            meta["topic"] = stem.lower()
            meta["source"] = f"{meta['country']}/{meta['city']}/{stem}"
            return meta
    except ValueError:
        pass

    # 구버전: corpus/*.md
    meta["city"] = stem.lower()
    meta["source"] = stem
    return meta


class RAGEngine:
    """FAISS 기반 경량 RAG 엔진"""

    def __init__(self):
        self.chunks: List[dict] = []
        self.index = None
        self._initialized = False
        self._embedding_provider = "openai"  # "openai" or "local"

    # ── 문서 로딩 & 청킹 ──────────────────────
    def _iter_md_files(self) -> List[str]:
        # 재귀 탐색 (구버전 flat도 같이 커버)
        return sorted(glob.glob(os.path.join(CORPUS_DIR, "**", "*.md"), recursive=True))

    def _load_corpus(self) -> List[dict]:
        """corpus/ 디렉토리의 모든 .md 파일을 청크로 분할"""
        chunks: List[dict] = []
        md_files = self._iter_md_files()

        for filepath in md_files:
            meta = _parse_meta_from_path(filepath)
            try:
                text = Path(filepath).read_text(encoding="utf-8")
            except UnicodeDecodeError:
                text = Path(filepath).read_text(encoding="utf-8-sig")

            sections = self._split_by_sections(text)
            for section in sections:
                sub_chunks = self._split_text(section, CHUNK_SIZE_WORDS, CHUNK_OVERLAP_WORDS)
                for chunk_text in sub_chunks:
                    chunk_text = chunk_text.strip()
                    if len(chunk_text) <= 20:
                        continue
                    chunks.append({
                        "text": chunk_text,
                        "source": meta["source"],       # 표시용(출처)
                        "country": meta["country"],
                        "city": meta["city"],
                        "topic": meta["topic"],
                        "path": str(Path(filepath).relative_to(Path(CORPUS_DIR)).as_posix()),
                    })

        logger.info(f"RAG corpus loaded: {len(chunks)} chunks from {len(md_files)} files")
        return chunks

    def _split_by_sections(self, text: str) -> List[str]:
        """마크다운 헤더(## / **) 기준으로 분할"""
        sections = []
        current = []
        for line in text.split("\n"):
            if (line.startswith("## ") or line.startswith("**")) and current:
                sections.append("\n".join(current))
                current = [line]
            else:
                current.append(line)
        if current:
            sections.append("\n".join(current))
        return sections if sections else [text]

    def _split_text(self, text: str, max_words: int, overlap_words: int) -> List[str]:
        """텍스트를 단어 기준으로 분할"""
        words = text.split()
        if len(words) <= max_words:
            return [text]

        chunks = []
        start = 0
        while start < len(words):
            end = start + max_words
            chunk = " ".join(words[start:end])
            chunks.append(chunk)
            if end >= len(words):
                break
            start = max(0, end - overlap_words)
        return chunks

    # ── 임베딩 생성 ──────────────────────────
    def _get_embeddings(self, texts: List[str]) -> np.ndarray:
        """임베딩 벡터 생성 (OpenAI 또는 로컬 모델)"""
        if self._embedding_provider == "local":
            return self._get_local_embeddings(texts)
        return self._get_openai_embeddings(texts)

    def _get_openai_embeddings(self, texts: List[str]) -> np.ndarray:
        """OpenAI 임베딩 API"""
        from openai import OpenAI
        from django.conf import settings

        client = OpenAI(api_key=settings.OPENAI_API_KEY)
        all_embeddings = []
        batch_size = 100

        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            response = client.embeddings.create(
                model=EMBEDDING_MODEL,
                input=batch,
            )
            batch_embeddings = [item.embedding for item in response.data]
            all_embeddings.extend(batch_embeddings)

        return np.array(all_embeddings, dtype="float32")

    def _get_local_embeddings(self, texts: List[str]) -> np.ndarray:
        """로컬 임베딩 모델 (sentence-transformers) - API 키 없이 사용 가능"""
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            raise ImportError("sentence-transformers가 필요합니다: pip install sentence-transformers") from e
        model = SentenceTransformer("intfloat/multilingual-e5-small")
        embeddings = model.encode(texts, show_progress_bar=False, normalize_embeddings=False)
        return np.array(embeddings, dtype="float32")

    # ── (선택) Rerank ────────────────────────
    def _rerank(self, query: str, candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Cross-encoder rerank (가능할 때만).
        - sentence-transformers CrossEncoder가 있으면 사용
        - 없으면 입력 순서 유지(= rerank 스킵)
        """
        if not ENABLE_RERANK:
            return candidates
        try:
            from sentence_transformers import CrossEncoder
        except Exception:
            logger.warning("Rerank enabled but CrossEncoder unavailable. Skip rerank.")
            return candidates

        try:
            ce = CrossEncoder(RERANK_MODEL)
            pairs = [(query, c["text"]) for c in candidates]
            scores = ce.predict(pairs)
            for c, s in zip(candidates, scores):
                c["_rerank"] = float(s)
            candidates.sort(key=lambda x: x.get("_rerank", 0.0), reverse=True)
            return candidates
        except Exception as e:
            logger.warning(f"Rerank failed. Skip rerank. reason={e}")
            return candidates

    # ── FAISS 인덱스 구축 ────────────────────
    def build_index(self, force_rebuild: bool = False):
        """인덱스 빌드 (캐시 지원)"""
        try:
            import faiss
        except ImportError as e:
            raise ImportError("faiss-cpu가 필요합니다: pip install faiss-cpu") from e

        os.makedirs(INDEX_DIR, exist_ok=True)
        logger.info(f"RAG index directory: {INDEX_DIR}")
        index_path = os.path.join(INDEX_DIR, "faiss.index")
        meta_path = os.path.join(INDEX_DIR, "chunks.json")
        hash_path = os.path.join(INDEX_DIR, "corpus.hash")

        corpus_hash = self._corpus_hash()

        # 캐시 확인
        if not force_rebuild and os.path.exists(index_path) and os.path.exists(hash_path):
            try:
                if Path(hash_path).read_text(encoding="utf-8").strip() == corpus_hash:
                    self.index = faiss.read_index(index_path)
                    self.chunks = json.loads(Path(meta_path).read_text(encoding="utf-8"))
                    self._initialized = True
                    logger.info(f"RAG index loaded from cache ({len(self.chunks)} chunks)")
                    return
            except Exception as e:
                logger.warning(f"Cache load failed, rebuilding: {e}")

        # 새로 빌드
        self.chunks = self._load_corpus()
        if not self.chunks:
            logger.warning("RAG corpus is empty!")
            self._initialized = True
            return

        texts = [c["text"] for c in self.chunks]
        embeddings = self._get_embeddings(texts)

        import faiss
        faiss.normalize_L2(embeddings)
        dim = embeddings.shape[1]
        self.index = faiss.IndexFlatIP(dim)
        self.index.add(embeddings)

        # 캐시 저장
        faiss.write_index(self.index, index_path)
        Path(meta_path).write_text(json.dumps(self.chunks, ensure_ascii=False, indent=2), encoding="utf-8")
        Path(hash_path).write_text(corpus_hash, encoding="utf-8")

        self._initialized = True
        logger.info(f"RAG index built: {len(self.chunks)} chunks, dim={dim}")

    def _corpus_hash(self) -> str:
        h = hashlib.md5()
        for fp in self._iter_md_files():
            with open(fp, "rb") as f:
                h.update(f.read())
        # 설정값도 해시에 포함(설정 바뀌면 재빌드)
        h.update(str(CHUNK_SIZE_WORDS).encode("utf-8"))
        h.update(str(CHUNK_OVERLAP_WORDS).encode("utf-8"))
        h.update(EMBEDDING_MODEL.encode("utf-8"))
        return h.hexdigest()

    # ── 검색 ─────────────────────────────────
    def search(
        self,
        query: str,
        retrieve_k: int = RAG_RETRIEVE_K,
        final_k: int = RAG_FINAL_K,
        country: Optional[str] = None,
        city: Optional[str] = None,
        topic: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        쿼리와 유사한 청크 검색.
        Returns: [{"text","source","score","country","city","topic","path"}, ...]
        """
        if not self._initialized:
            self.build_index()

        if not self.index or not self.chunks:
            return []

        import faiss

        query_embedding = self._get_embeddings([query])
        faiss.normalize_L2(query_embedding)

        # 후보는 넓게 가져오기
        k = min(max(retrieve_k, final_k), len(self.chunks))
        scores, indices = self.index.search(query_embedding, k)

        results: List[Dict[str, Any]] = []
        country_l = country.lower() if country else None
        city_l = city.lower() if city else None
        topic_l = topic.lower() if topic else None

        for score, idx in zip(scores[0], indices[0]):
            if idx < 0:
                continue
            c = self.chunks[idx]
            if country_l and (c.get("country") or "").lower() != country_l:
                continue
            if city_l and (c.get("city") or "").lower() != city_l:
                continue
            if topic_l and (c.get("topic") or "").lower() != topic_l:
                continue
            results.append({**c, "score": float(score)})
            if len(results) >= k:
                break

        # rerank (선택)
        results = self._rerank(query, results)

        return results[:final_k]


# ── 싱글턴 & 편의 함수 ───────────────────────
_engine: Optional[RAGEngine] = None


def get_rag_engine() -> RAGEngine:
    global _engine
    if _engine is None:
        _engine = RAGEngine()
    return _engine


def rag_search(
    query: str,
    top_k: int = TOP_K,
    city: str = None,
    country: str = None,
    topic: str = None,
    retrieve_k: int = None,
    final_k: int = None,
) -> List[Dict[str, Any]]:
    """
    하위호환 + 확장
    - 기존: rag_search(query, top_k=5, city="tokyo")
    - 신규: rag_search(query, country="jp", city="tokyo", topic="transport", retrieve_k=40, final_k=8)
    """
    engine = get_rag_engine()
    if final_k is None:
        final_k = top_k
    if retrieve_k is None:
        retrieve_k = max(final_k * 3, RAG_RETRIEVE_K)
    return engine.search(
        query=query,
        retrieve_k=retrieve_k,
        final_k=final_k,
        country=country,
        city=city,
        topic=topic,
    )
