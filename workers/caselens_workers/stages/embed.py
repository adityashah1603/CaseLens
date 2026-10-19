"""Shared `embed` stage: one 768-dim embedding per chunk (BAAI/bge-base-en-v1.5 via fastembed)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from functools import lru_cache
from typing import Any

from caselens_workers.context import StageContext, StageResult
from caselens_workers.db import vector_literal

Embedder = Callable[[Sequence[str]], list[list[float]]]
EMBEDDING_DIM = 768
BATCH = 32


@lru_cache(maxsize=2)
def _fastembed(model: str, cache_dir: str, threads: int) -> Any:
    from fastembed import TextEmbedding

    return TextEmbedding(model_name=model, cache_dir=cache_dir, threads=threads)


def default_embedder(model: str, cache_dir: str, threads: int) -> Embedder:
    def embed(texts: Sequence[str]) -> list[list[float]]:
        engine = _fastembed(model, cache_dir, threads)
        return [list(map(float, v)) for v in engine.embed(list(texts), batch_size=BATCH)]

    return embed


def embed_texts(ctx: StageContext, texts: Sequence[str]) -> list[list[float]]:
    """Embed with the configured model (also used by retrieval for queries)."""
    s = ctx.settings
    return default_embedder(s.embedding_model, str(s.model_cache_dir), s.model_threads)(texts)


def make_embed_stage(embedder: Embedder | None = None) -> Callable[[StageContext], StageResult]:
    def embed(ctx: StageContext) -> StageResult:
        rows = ctx.conn.execute(
            "SELECT id, coalesce(text, '') FROM public.chunks WHERE evidence_id = %s "
            "ORDER BY chunk_index",
            (ctx.evidence.id,),
        ).fetchall()
        texts = [r[1] for r in rows]
        vectors = (embedder or (lambda t: embed_texts(ctx, t)))(texts) if texts else []
        for (chunk_id, _), vec in zip(rows, vectors, strict=True):
            if len(vec) != EMBEDDING_DIM:
                raise ValueError(f"embedding has {len(vec)} dims, schema expects {EMBEDDING_DIM}")
            ctx.conn.execute(
                "UPDATE public.chunks SET embedding = %s::vector WHERE id = %s",
                (vector_literal(vec), chunk_id),
            )
        return StageResult(summary={"chunks_embedded": len(rows)})

    return embed


embed = make_embed_stage()
