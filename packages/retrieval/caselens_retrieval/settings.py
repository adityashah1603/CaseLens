"""Retrieval configuration from CASELENS_* environment variables."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

RERANK_DISABLED = "none"


class RetrievalSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CASELENS_", extra="ignore")

    model_cache_dir: Path = Path(".cache/models")
    # Must match the ingest `embed` stage (workers' CASELENS_EMBEDDING_MODEL).
    embedding_model: str = "BAAI/bge-base-en-v1.5"
    # Prepended to queries before embedding. Empty by default: on the demo case the bge query
    # instruction did not improve recall (see README).
    query_instruction: str = ""
    # fastembed TextCrossEncoder model name, or "none" to disable reranking.
    rerank_model: str = "Xenova/ms-marco-MiniLM-L-6-v2"
    model_threads: int = Field(default=2, ge=1)
    # Candidates taken from each of the vector and full-text lists before fusion.
    retrieval_candidates: int = Field(default=40, ge=1, le=500)
    # Fused candidates passed to the cross-encoder.
    rerank_candidates: int = Field(default=30, ge=1, le=200)
    # Long chunks are reranked on their best-matching window of this many words (0 = full text).
    rerank_window_words: int = Field(default=48, ge=0)
    # Small batches waste less time on padding on CPU (measured: 1 beat 8 and 64).
    rerank_batch_size: int = Field(default=1, ge=1, le=256)
    rrf_k: int = Field(default=60, ge=1)
    hnsw_ef_search: int = Field(default=100, ge=1, le=1000)
    # Record lane: structured-record rows matching the phones / plates / item tags / local time
    # window a query names are added to the results (see `hints.py`). Wall-clock times in
    # queries are read in this timezone (the same env var as the agent's).
    record_lane: bool = True
    case_timezone: str = "America/Los_Angeles"


@lru_cache(maxsize=1)
def get_settings() -> RetrievalSettings:
    return RetrievalSettings()
