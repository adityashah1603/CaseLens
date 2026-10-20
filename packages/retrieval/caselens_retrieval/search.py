"""Hybrid search: pgvector cosine + Postgres full-text, fused with RRF, then cross-encoder rerank.

The connection must already run as `caselens_app` with `app.user_id` set, so RLS applies on top
of the explicit, bound `case_id` predicate. All values are bound parameters; the only composed
SQL is fixed fragments from this module (`psycopg.sql.SQL` literals).
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg import sql

from caselens_retrieval.encoders import (
    EMBEDDING_DIM,
    Embedder,
    Reranker,
    default_embedder,
    default_reranker,
)
from caselens_retrieval.models import (
    LOCATOR_ADAPTER,
    EvidenceFilters,
    EvidenceResult,
    Keyframe,
    Modality,
    SearchEvidenceOutput,
    SearchVideoOutput,
    VideoLocator,
    VideoResult,
)
from caselens_retrieval.settings import RetrievalSettings, get_settings

Conn = psycopg.Connection[Any]

MAX_TOP_K = 20
_COLUMNS = sql.SQL(
    "c.id, c.evidence_id, c.modality, coalesce(c.text, ''), c.locator, c.t_start, c.t_end"
)
# Characters that carry meaning in websearch_to_tsquery syntax are dropped from the terms.
_TERM_RE = re.compile(r"[^\s\"'()\-]+")
_WORD_RE = re.compile(r"\w+")
_STOPWORDS = frozenset(
    _WORD_RE.findall(
        "a an and are at be by did do does for from had has have he her him his how in is it its "
        "me my of on or she that the their them they this to was were what when where which who "
        "whom why with you your"
    )
)


@dataclass
class SearchTimings:
    """Filled in by a search call when passed as `timings=` (milliseconds)."""

    embed_ms: float = 0.0
    sql_ms: float = 0.0
    rerank_ms: float = 0.0
    candidates: int = 0


@dataclass(frozen=True)
class Candidate:
    chunk_id: uuid.UUID
    evidence_id: uuid.UUID
    modality: Modality
    text: str
    locator: dict[str, Any]
    t_start: datetime | None
    t_end: datetime | None


@dataclass(frozen=True)
class Scored:
    candidate: Candidate
    score: float


def rrf_fuse(rankings: Sequence[Sequence[uuid.UUID]], k: int = 60) -> list[tuple[uuid.UUID, float]]:
    """Reciprocal rank fusion: score(d) = sum over lists of 1 / (k + rank), rank from 1.

    Ties break on the best single-list rank, then on the id, so the order is deterministic.
    """
    scores: dict[uuid.UUID, float] = {}
    best: dict[uuid.UUID, int] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
            best[item] = min(best.get(item, rank), rank)
    return sorted(scores.items(), key=lambda kv: (-kv[1], best[kv[0]], str(kv[0])))


def fts_or_query(query: str) -> str:
    """Turn a natural-language question into an OR of its terms for websearch_to_tsquery.

    websearch_to_tsquery ANDs bare words, so "Who was in the gray sedan on March 14?" would only
    match chunks containing every term. ORing them lets ts_rank_cd rank partial matches; the
    vector list and the reranker handle meaning.
    """
    terms = [t for t in _TERM_RE.findall(query) if t.lower() != "or" and _WORD_RE.search(t)]
    return " or ".join(terms)


def _stem(word: str) -> str:
    return word.lower()[:5]


def rerank_snippet(query: str, text: str, window_words: int) -> str:
    """The window of `text` (about `window_words` words) sharing the most query terms.

    Cross-encoders cost grows with input length and they truncate at 512 tokens, so a long PDF
    page is scored on its best-matching window instead of on its (possibly cut-off) head.
    Matching is on crude 5-letter stems; ties keep the earliest window.
    """
    words = text.split()
    if window_words <= 0 or len(words) <= window_words:
        return text
    terms = {_stem(w) for w in _WORD_RE.findall(query) if w.lower() not in _STOPWORDS}
    stride = max(window_words // 2, 1)
    best_start, best_hits = 0, -1
    for start in range(0, len(words) - stride, stride):
        window = words[start : start + window_words]
        hits = len(terms & {_stem(w) for tok in window for w in _WORD_RE.findall(tok)})
        if hits > best_hits:
            best_start, best_hits = start, hits
    return " ".join(words[best_start : best_start + window_words])


def _vector_literal(vec: Sequence[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def _filter_clauses(
    case_id: uuid.UUID, filters: EvidenceFilters | None
) -> tuple[list[sql.Composable], dict[str, Any]]:
    clauses: list[sql.Composable] = [sql.SQL("c.case_id = %(case_id)s")]
    params: dict[str, Any] = {"case_id": case_id}
    if filters is None:
        return clauses, params
    if filters.modalities:
        clauses.append(sql.SQL("c.modality = ANY(%(modalities)s::text[])"))
        params["modalities"] = list(filters.modalities)
    if filters.evidence_ids:
        clauses.append(sql.SQL("c.evidence_id = ANY(%(evidence_ids)s::uuid[])"))
        params["evidence_ids"] = list(filters.evidence_ids)
    if filters.entity_ids:
        # Mentions of the given entities or of any entity joined to them by active links,
        # followed in both directions (the transitive closure of the merge graph).
        clauses.append(
            sql.SQL(
                "EXISTS (SELECT 1 FROM public.mentions m WHERE m.chunk_id = c.id "
                "AND m.case_id = %(case_id)s AND m.entity_id IN ("
                "WITH RECURSIVE linked(id) AS ("
                "SELECT unnest(%(entity_ids)s::uuid[]) "
                "UNION "
                "SELECT CASE WHEN l.from_entity_id = linked.id THEN l.to_entity_id "
                "ELSE l.from_entity_id END "
                "FROM public.entity_links l JOIN linked "
                "ON l.from_entity_id = linked.id OR l.to_entity_id = linked.id "
                "WHERE l.case_id = %(case_id)s AND l.status = 'active') "
                "SELECT id FROM linked))"
            )
        )
        params["entity_ids"] = list(filters.entity_ids)
    if filters.time_from is not None or filters.time_to is not None:
        # Timed chunks overlap the window; untimed ones (PDF pages) are kept only when an
        # event inside the window cites them.
        clauses.append(
            sql.SQL(
                "((c.t_start IS NOT NULL "
                "AND c.t_start <= coalesce(%(time_to)s::timestamptz, 'infinity') "
                "AND coalesce(c.t_end, c.t_start) >= "
                "coalesce(%(time_from)s::timestamptz, '-infinity')) "
                "OR (c.t_start IS NULL AND EXISTS (SELECT 1 FROM public.events e "
                "WHERE e.case_id = %(case_id)s AND c.id = ANY(e.source_chunk_ids) "
                "AND e.t_start <= coalesce(%(time_to)s::timestamptz, 'infinity') "
                "AND coalesce(e.t_end, e.t_start) >= "
                "coalesce(%(time_from)s::timestamptz, '-infinity'))))"
            )
        )
        params["time_from"] = filters.time_from
        params["time_to"] = filters.time_to
    return clauses, params


def _candidate(row: tuple[Any, ...]) -> Candidate:
    return Candidate(
        chunk_id=row[0],
        evidence_id=row[1],
        modality=row[2],
        text=row[3],
        locator=row[4],
        t_start=row[5],
        t_end=row[6],
    )


def _retrieve(
    conn: Conn,
    case_id: uuid.UUID,
    query_vec: Sequence[float] | None,
    fts_query: str,
    filters: EvidenceFilters | None,
    settings: RetrievalSettings,
) -> tuple[list[Candidate], list[Candidate]]:
    clauses, params = _filter_clauses(case_id, filters)
    where = sql.SQL(" AND ").join(clauses)
    params["n"] = settings.retrieval_candidates
    vector_hits: list[Candidate] = []
    text_hits: list[Candidate] = []
    with conn.transaction():
        # Iterative HNSW scans keep filtered searches from returning too few rows (pgvector
        # >= 0.8). relaxed_order may emit rows slightly out of order, so re-sort by distance.
        conn.execute(
            "SELECT set_config('hnsw.ef_search', %s, true), "
            "set_config('hnsw.iterative_scan', 'relaxed_order', true)",
            (str(settings.hnsw_ef_search),),
        )
        if query_vec is not None:
            stmt = sql.SQL(
                "SELECT {cols}, c.embedding <=> %(qvec)s::vector AS dist FROM public.chunks c "
                "WHERE {where} AND c.embedding IS NOT NULL "
                "ORDER BY c.embedding <=> %(qvec)s::vector LIMIT %(n)s"
            ).format(cols=_COLUMNS, where=where)
            rows = conn.execute(stmt, {**params, "qvec": _vector_literal(query_vec)}).fetchall()
            rows.sort(key=lambda r: (r[7], str(r[0])))
            vector_hits = [_candidate(r) for r in rows]
        if fts_query:
            stmt = sql.SQL(
                "SELECT {cols}, ts_rank_cd(c.tsv, q) AS rank FROM public.chunks c, "
                "websearch_to_tsquery('english', %(fts)s) q "
                "WHERE {where} AND c.tsv @@ q ORDER BY rank DESC, c.id LIMIT %(n)s"
            ).format(cols=_COLUMNS, where=where)
            rows = conn.execute(stmt, {**params, "fts": fts_query}).fetchall()
            text_hits = [_candidate(r) for r in rows]
    return vector_hits, text_hits


def hybrid_search(
    conn: Conn,
    case_id: uuid.UUID,
    query: str,
    filters: EvidenceFilters | None = None,
    top_k: int = 8,
    *,
    embedder: Embedder | None = None,
    reranker: Reranker | None = None,
    rerank: bool = True,
    timings: SearchTimings | None = None,
    settings: RetrievalSettings | None = None,
) -> list[Scored]:
    """Vector + full-text candidates, RRF (k=60), cross-encoder rerank, best first."""
    if not 1 <= top_k <= MAX_TOP_K:
        raise ValueError(f"top_k must be between 1 and {MAX_TOP_K}")
    if not query.strip():
        raise ValueError("query must not be empty")
    s = settings or get_settings()
    t = timings or SearchTimings()

    t0 = time.perf_counter()
    query_vec = (embedder or default_embedder())(query)
    if len(query_vec) != EMBEDDING_DIM:
        raise ValueError(f"query embedding has {len(query_vec)} dims, expected {EMBEDDING_DIM}")
    t1 = time.perf_counter()
    vector_hits, text_hits = _retrieve(conn, case_id, query_vec, fts_or_query(query), filters, s)
    t2 = time.perf_counter()

    by_id = {c.chunk_id: c for c in (*vector_hits, *text_hits)}
    fused = rrf_fuse(
        [[c.chunk_id for c in vector_hits], [c.chunk_id for c in text_hits]], k=s.rrf_k
    )
    ranker = (reranker or default_reranker()) if rerank else None
    if ranker is None or not fused:
        results = [Scored(by_id[cid], score) for cid, score in fused[:top_k]]
    else:
        pool = [by_id[cid] for cid, _ in fused[: s.rerank_candidates]]
        scores = ranker(query, [rerank_snippet(query, c.text, s.rerank_window_words) for c in pool])
        if len(scores) != len(pool):
            raise ValueError("reranker returned the wrong number of scores")
        order = sorted(range(len(pool)), key=lambda i: (-scores[i], i))
        results = [Scored(pool[i], float(scores[i])) for i in order[:top_k]]
    t3 = time.perf_counter()

    t.embed_ms = (t1 - t0) * 1000
    t.sql_ms = (t2 - t1) * 1000
    t.rerank_ms = (t3 - t2) * 1000
    t.candidates = len(fused)
    return results


def search_evidence(
    conn: Conn,
    case_id: uuid.UUID,
    query: str,
    filters: EvidenceFilters | None = None,
    top_k: int = 8,
    *,
    embedder: Embedder | None = None,
    reranker: Reranker | None = None,
    rerank: bool = True,
    timings: SearchTimings | None = None,
) -> SearchEvidenceOutput:
    """`search_evidence` tool: ranked chunks of the case with their stored locators."""
    hits = hybrid_search(
        conn,
        case_id,
        query,
        filters,
        top_k,
        embedder=embedder,
        reranker=reranker,
        rerank=rerank,
        timings=timings,
    )
    return SearchEvidenceOutput(
        results=[
            EvidenceResult(
                chunk_id=h.candidate.chunk_id,
                evidence_id=h.candidate.evidence_id,
                modality=h.candidate.modality,
                text=h.candidate.text,
                score=h.score,
                locator=LOCATOR_ADAPTER.validate_python(h.candidate.locator),
            )
            for h in hits
        ]
    )


def search_video(
    conn: Conn,
    case_id: uuid.UUID,
    query: str,
    time_from: datetime | None = None,
    time_to: datetime | None = None,
    evidence_ids: Sequence[uuid.UUID] | None = None,
    top_k: int = 8,
    *,
    embedder: Embedder | None = None,
    reranker: Reranker | None = None,
    rerank: bool = True,
    timings: SearchTimings | None = None,
) -> SearchVideoOutput:
    """`search_video` tool: the hybrid pipeline over video chunks (track summaries, captions).

    A given time window is a hard filter on wall-clock overlap. Keyframes come from the chunk
    locator; `image_uri` is null until keyframe images are written to object storage.
    """
    filters = EvidenceFilters(
        modalities=["video"],
        evidence_ids=list(evidence_ids) if evidence_ids else None,
        time_from=time_from,
        time_to=time_to,
    )
    hits = hybrid_search(
        conn,
        case_id,
        query,
        filters,
        top_k,
        embedder=embedder,
        reranker=reranker,
        rerank=rerank,
        timings=timings,
    )
    results: list[VideoResult] = []
    for h in hits:
        c = h.candidate
        loc = VideoLocator.model_validate(c.locator)
        keyframes = (
            [Keyframe(frame_idx=loc.frame_idx, bbox=loc.bbox, image_uri=None)]
            if loc.frame_idx is not None
            else []
        )
        results.append(
            VideoResult(
                chunk_id=c.chunk_id,
                evidence_id=c.evidence_id,
                track_id=loc.track_id,
                summary=c.text,
                start_ms=loc.start_ms,
                end_ms=loc.end_ms,
                wall_clock_start=c.t_start,
                wall_clock_end=c.t_end,
                keyframes=keyframes,
                score=h.score,
                locator=loc,
            )
        )
    return SearchVideoOutput(results=results)
