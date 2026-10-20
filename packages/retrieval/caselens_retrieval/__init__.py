"""CaseLens hybrid retrieval (pgvector + full-text, RRF, rerank)."""

from caselens_retrieval.encoders import (
    EMBEDDING_DIM,
    Embedder,
    Reranker,
    embed_query,
    set_default_embedder,
    set_default_reranker,
)
from caselens_retrieval.models import (
    AudioLocator,
    EvidenceFilters,
    EvidenceResult,
    Keyframe,
    Locator,
    PdfLocator,
    RecordLocator,
    SearchEvidenceInput,
    SearchEvidenceOutput,
    SearchVideoInput,
    SearchVideoOutput,
    VideoLocator,
    VideoResult,
)
from caselens_retrieval.search import (
    SearchTimings,
    hybrid_search,
    rrf_fuse,
    search_evidence,
    search_video,
)
from caselens_retrieval.settings import RetrievalSettings, get_settings

__version__ = "0.0.1"

__all__ = [
    "EMBEDDING_DIM",
    "AudioLocator",
    "Embedder",
    "EvidenceFilters",
    "EvidenceResult",
    "Keyframe",
    "Locator",
    "PdfLocator",
    "RecordLocator",
    "Reranker",
    "RetrievalSettings",
    "SearchEvidenceInput",
    "SearchEvidenceOutput",
    "SearchTimings",
    "SearchVideoInput",
    "SearchVideoOutput",
    "VideoLocator",
    "VideoResult",
    "__version__",
    "embed_query",
    "get_settings",
    "hybrid_search",
    "rrf_fuse",
    "search_evidence",
    "search_video",
    "set_default_embedder",
    "set_default_reranker",
]
