"""
Retrieval over everything this system has read.

The committee and the analyst pass both work from an evidence block, and the
quality of that block decides the quality of everything downstream. A model
given three stale headlines will write a confident thesis from three stale
headlines. So the corpus here is everything the system has actually seen: news
items, past memos, disclosed trades, committee transcripts, filing extracts.

Retrieval is hybrid, and deliberately so. Dense embeddings find "margin
compression" when the query says "profitability falling", which lexical search
never will. Lexical search finds "AVGO" when the query says "AVGO", which
embeddings are surprisingly bad at, because a ticker is a rare token that
carries almost no semantic weight. Ranking is fused with reciprocal rank
fusion, which needs no score calibration between the two and therefore has no
tuning knob to get wrong.

The embedder is pluggable and the active one is always reported. If the ONNX
model is not installed the index still works, on hashed character n-grams,
which is fuzzy lexical matching and not semantics. That distinction is
reported rather than hidden, because "semantic search" that is quietly doing
substring matching is the kind of thing that looks fine until it matters.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np

from .config import DATA_DIR

log = logging.getLogger(__name__)

INDEX_DIR = DATA_DIR / "rag"
DOCS_PATH = INDEX_DIR / "docs.jsonl"
VECS_PATH = INDEX_DIR / "vectors.npy"

CHUNK_CHARS = 900
CHUNK_OVERLAP = 150
HASH_DIMS = 512

_EMBEDDER: "Embedder | None" = None


# --------------------------------------------------------------------------
# documents
# --------------------------------------------------------------------------

@dataclass
class Document:
    id: str
    text: str
    kind: str                 # news | memo | disclosure | committee | filing
    symbol: str = ""
    at: str = ""              # ISO timestamp, used for recency weighting
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Hit:
    document: Document
    score: float
    why: str = ""


def _chunk(text: str) -> list[str]:
    """
    Overlapping windows, split on sentence boundaries where possible.

    The overlap exists so a fact that straddles a boundary is retrievable from
    either side. Without it, the single most useful sentence in a document is
    the one most likely to be cut in half.
    """
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= CHUNK_CHARS:
        return [text] if text else []

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + CHUNK_CHARS, len(text))
        if end < len(text):
            window = text.rfind(". ", start + CHUNK_CHARS // 2, end)
            if window != -1:
                end = window + 1
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = max(end - CHUNK_OVERLAP, start + 1)
    return chunks


def _doc_id(kind: str, symbol: str, text: str, ordinal: int) -> str:
    digest = hashlib.sha1(f"{kind}|{symbol}|{text}".encode()).hexdigest()[:16]
    return f"{digest}-{ordinal}"


# --------------------------------------------------------------------------
# embedders
# --------------------------------------------------------------------------

class Embedder:
    name = "none"
    semantic = False
    dims = HASH_DIMS

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        raise NotImplementedError


class HashEmbedder(Embedder):
    """
    Deterministic character n-gram hashing.

    This is a fallback, not a model. It matches on shared spelling, so it finds
    "margin" from "margins" and nothing at all from "profitability". It is here
    so the index keeps working with no model installed, and it reports itself
    honestly so nobody mistakes it for meaning.
    """
    name = "hashed character n-grams (lexical, not semantic)"
    semantic = False

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        out = np.zeros((len(texts), HASH_DIMS), dtype=np.float32)
        for row, text in enumerate(texts):
            norm = _normalise(text)
            for token in norm.split():
                for n in (3, 4):
                    for i in range(max(len(token) - n + 1, 1)):
                        gram = token[i:i + n]
                        h = int(hashlib.md5(gram.encode()).hexdigest()[:8], 16)
                        out[row, h % HASH_DIMS] += 1.0
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        return out / np.maximum(norms, 1e-9)


class FastEmbedEmbedder(Embedder):
    """BAAI/bge-small-en-v1.5 through ONNX. CPU only, no torch."""
    name = "bge-small-en-v1.5"
    semantic = True

    def __init__(self) -> None:
        from fastembed import TextEmbedding
        self._model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
        self.dims = 384

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        vecs = np.array(list(self._model.embed(list(texts))), dtype=np.float32)
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        return vecs / np.maximum(norms, 1e-9)


def embedder() -> Embedder:
    global _EMBEDDER
    if _EMBEDDER is None:
        try:
            _EMBEDDER = FastEmbedEmbedder()
            log.info("rag using %s", _EMBEDDER.name)
        except Exception as e:  # noqa: BLE001 - the index must work regardless
            log.warning("fastembed unavailable (%s), falling back to hashing", e)
            _EMBEDDER = HashEmbedder()
    return _EMBEDDER


def backend_name() -> str:
    e = embedder()
    return e.name if e.semantic else f"{e.name} [degraded]"


# --------------------------------------------------------------------------
# lexical half
# --------------------------------------------------------------------------

_STOP = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "are",
    "was", "were", "be", "been", "it", "its", "this", "that", "with", "as",
    "at", "by", "from", "has", "have", "had", "but", "not", "will", "would",
}


def _normalise(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").lower()
    return re.sub(r"[^a-z0-9 ]+", " ", text)


def _tokens(text: str) -> list[str]:
    return [t for t in _normalise(text).split() if t and t not in _STOP]


class BM25:
    """
    Okapi BM25. Rebuilt on load rather than persisted: it is a few counters
    over a corpus this size, and a stale posting list would be a silent
    correctness bug for the sake of saving milliseconds.
    """

    def __init__(self, corpus: Sequence[Sequence[str]], k1: float = 1.5,
                 b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.docs = [Counter(doc) for doc in corpus]
        self.lengths = np.array([sum(c.values()) for c in self.docs], dtype=np.float32)
        self.avg_len = float(self.lengths.mean()) if len(self.lengths) else 0.0
        self.df: Counter[str] = Counter()
        for counts in self.docs:
            self.df.update(counts.keys())
        self.n = len(self.docs)

    def scores(self, query: Sequence[str]) -> np.ndarray:
        out = np.zeros(self.n, dtype=np.float32)
        if not self.n or self.avg_len <= 0:
            return out
        for term in query:
            df = self.df.get(term, 0)
            if not df:
                continue
            idf = math.log(1 + (self.n - df + 0.5) / (df + 0.5))
            for i, counts in enumerate(self.docs):
                tf = counts.get(term, 0)
                if not tf:
                    continue
                denom = tf + self.k1 * (
                    1 - self.b + self.b * self.lengths[i] / self.avg_len)
                out[i] += idf * (tf * (self.k1 + 1)) / denom
        return out


# --------------------------------------------------------------------------
# the index
# --------------------------------------------------------------------------

class Index:
    def __init__(self) -> None:
        self.documents: list[Document] = []
        self.vectors: np.ndarray = np.zeros((0, embedder().dims), dtype=np.float32)
        self._bm25: BM25 | None = None
        self._seen: set[str] = set()

    # -- persistence ------------------------------------------------------

    def load(self) -> "Index":
        if not DOCS_PATH.exists():
            return self
        try:
            rows = [json.loads(line) for line in
                    DOCS_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.documents = [Document(**r) for r in rows]
            self._seen = {d.id for d in self.documents}
            if VECS_PATH.exists():
                self.vectors = np.load(VECS_PATH)
        except Exception as e:  # noqa: BLE001
            log.warning("rag index unreadable, starting empty: %s", e)
            self.documents, self.vectors, self._seen = [], np.zeros(
                (0, embedder().dims), dtype=np.float32), set()

        # A vector file that does not line up with the document file means one
        # of the two was written and the other was not. Rebuilding is cheap and
        # searching a misaligned index would return confidently wrong passages.
        if len(self.documents) != len(self.vectors):
            log.warning("rag index misaligned (%d docs, %d vectors), re-embedding",
                        len(self.documents), len(self.vectors))
            self._reembed()
        self._bm25 = None
        return self

    def save(self) -> None:
        INDEX_DIR.mkdir(parents=True, exist_ok=True)
        with DOCS_PATH.open("w", encoding="utf-8") as fh:
            for doc in self.documents:
                fh.write(json.dumps(doc.to_dict(), default=str) + "\n")
        np.save(VECS_PATH, self.vectors)

    def _reembed(self) -> None:
        if not self.documents:
            self.vectors = np.zeros((0, embedder().dims), dtype=np.float32)
            return
        self.vectors = embedder().encode([d.text for d in self.documents])

    # -- writing ----------------------------------------------------------

    def add(self, kind: str, text: str, *, symbol: str = "", at: str = "",
            meta: dict[str, Any] | None = None) -> int:
        """Chunk, embed and store. Returns how many chunks were new."""
        chunks = _chunk(text)
        fresh: list[Document] = []
        for i, chunk in enumerate(chunks):
            doc_id = _doc_id(kind, symbol, chunk, i)
            if doc_id in self._seen:
                continue
            self._seen.add(doc_id)
            fresh.append(Document(id=doc_id, text=chunk, kind=kind,
                                  symbol=symbol.upper(), at=at,
                                  meta=meta or {}))
        if not fresh:
            return 0

        vecs = embedder().encode([d.text for d in fresh])
        self.documents.extend(fresh)
        self.vectors = np.vstack([self.vectors, vecs]) if len(self.vectors) else vecs
        self._bm25 = None
        return len(fresh)

    def prune(self, keep: int = 8000) -> int:
        """Oldest first. An index that grows forever eventually stops loading."""
        if len(self.documents) <= keep:
            return 0
        order = sorted(range(len(self.documents)),
                       key=lambda i: self.documents[i].at or "")
        drop = set(order[:len(self.documents) - keep])
        self.documents = [d for i, d in enumerate(self.documents) if i not in drop]
        self.vectors = np.delete(self.vectors, sorted(drop), axis=0)
        self._seen = {d.id for d in self.documents}
        self._bm25 = None
        return len(drop)

    # -- reading ----------------------------------------------------------

    def search(self, query: str, *, k: int = 8, symbol: str | None = None,
               kinds: Iterable[str] | None = None) -> list[Hit]:
        if not self.documents:
            return []

        allowed = [
            i for i, d in enumerate(self.documents)
            if (symbol is None or d.symbol == symbol.upper() or not d.symbol)
            and (kinds is None or d.kind in set(kinds))
        ]
        if not allowed:
            return []

        qvec = embedder().encode([query])[0]
        dense = self.vectors[allowed] @ qvec

        if self._bm25 is None:
            self._bm25 = BM25([_tokens(d.text) for d in self.documents])
        lexical = self._bm25.scores(_tokens(query))[allowed]

        # Reciprocal rank fusion. Ranks are comparable across two scoring
        # schemes in a way the raw scores are not, so nothing has to be
        # normalised and there is no weighting constant to tune wrong.
        fused = np.zeros(len(allowed), dtype=np.float32)
        for scores in (dense, lexical):
            order = np.argsort(-scores)
            for rank, idx in enumerate(order):
                fused[idx] += 1.0 / (60 + rank)

        top = np.argsort(-fused)[:k]
        hits: list[Hit] = []
        for pos in top:
            doc = self.documents[allowed[pos]]
            hits.append(Hit(
                document=doc, score=round(float(fused[pos]), 6),
                why=f"dense {float(dense[pos]):.3f}, bm25 {float(lexical[pos]):.2f}",
            ))
        return hits

    def stats(self) -> dict[str, Any]:
        kinds = Counter(d.kind for d in self.documents)
        return {
            "documents": len(self.documents),
            "embedder": backend_name(),
            "semantic": embedder().semantic,
            "dims": int(self.vectors.shape[1]) if len(self.vectors) else embedder().dims,
            "by_kind": dict(kinds),
            "symbols": len({d.symbol for d in self.documents if d.symbol}),
        }


# --------------------------------------------------------------------------
# module level convenience
# --------------------------------------------------------------------------

_INDEX: Index | None = None


def index() -> Index:
    global _INDEX
    if _INDEX is None:
        _INDEX = Index().load()
    return _INDEX


def reset() -> None:
    """Used by tests so one test's corpus is not another's."""
    global _INDEX
    _INDEX = None


def context_for(symbol: str, question: str, *, k: int = 6) -> list[dict[str, Any]]:
    """
    The retrieval the committee and analyst pass actually call.

    Returns plain dicts with the passage and its provenance, because a passage
    handed to a model without a source is a passage it will cite as fact
    without being able to say where it came from.
    """
    hits = index().search(question, k=k, symbol=symbol)
    return [
        {
            "text": h.document.text,
            "kind": h.document.kind,
            "symbol": h.document.symbol,
            "at": h.document.at,
            "source": h.document.meta.get("source", ""),
            "url": h.document.meta.get("url", ""),
            "relevance": h.score,
        }
        for h in hits
    ]
