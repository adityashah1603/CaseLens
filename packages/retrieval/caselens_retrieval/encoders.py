"""Query embedder and cross-encoder reranker (fastembed, CPU, loaded once per process).

Both are plain callables so tests and other packages can inject fakes, either per call
(`search_evidence(..., embedder=..., reranker=...)`) or process-wide with `set_default_embedder`
/ `set_default_reranker`.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from functools import lru_cache
from typing import Any

from caselens_retrieval.settings import RERANK_DISABLED, get_settings

EMBEDDING_DIM = 768

Embedder = Callable[[str], list[float]]
# (query, documents) -> one relevance score per document, higher is better.
Reranker = Callable[[str, Sequence[str]], list[float]]

_embedder_override: Embedder | None = None
_reranker_override: Reranker | None = None


@lru_cache(maxsize=2)
def _text_embedding(model: str, cache_dir: str, threads: int) -> Any:
    from fastembed import TextEmbedding

    return TextEmbedding(model_name=model, cache_dir=cache_dir, threads=threads)


@lru_cache(maxsize=2)
def _cross_encoder(model: str, cache_dir: str, threads: int) -> Any:
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    return TextCrossEncoder(model_name=model, cache_dir=cache_dir, threads=threads)


def fastembed_embed_query(text: str) -> list[float]:
    s = get_settings()
    engine = _text_embedding(s.embedding_model, str(s.model_cache_dir), s.model_threads)
    vectors = list(engine.embed([s.query_instruction + text]))
    vec = [float(x) for x in vectors[0]]
    if len(vec) != EMBEDDING_DIM:
        raise ValueError(f"embedding has {len(vec)} dims, expected {EMBEDDING_DIM}")
    return vec


def fastembed_rerank(query: str, documents: Sequence[str]) -> list[float]:
    s = get_settings()
    engine = _cross_encoder(s.rerank_model, str(s.model_cache_dir), s.model_threads)
    # Batches are padded to their longest pair, so score documents grouped by length.
    order = sorted(range(len(documents)), key=lambda i: len(documents[i]))
    scores = engine.rerank(query, [documents[i] for i in order], batch_size=s.rerank_batch_size)
    out = [0.0] * len(documents)
    for i, score in zip(order, scores, strict=True):
        out[i] = float(score)
    return out


def set_default_embedder(embedder: Embedder | None) -> None:
    """Replace the process-wide query embedder (None restores fastembed)."""
    global _embedder_override
    _embedder_override = embedder


def set_default_reranker(reranker: Reranker | None) -> None:
    """Replace the process-wide reranker (None restores the configured fastembed model)."""
    global _reranker_override
    _reranker_override = reranker


def default_embedder() -> Embedder:
    return _embedder_override or fastembed_embed_query


def default_reranker() -> Reranker | None:
    """The configured reranker, or None when CASELENS_RERANK_MODEL=none."""
    if _reranker_override is not None:
        return _reranker_override
    if get_settings().rerank_model.strip().lower() == RERANK_DISABLED:
        return None
    return fastembed_rerank


def embed_query(text: str) -> list[float]:
    """Embed a query with the ingest model (768-dim BAAI/bge-base-en-v1.5 by default)."""
    return default_embedder()(text)
