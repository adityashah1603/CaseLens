"""Hybrid search: pgvector cosine + Postgres full-text, fused with RRF, then cross-encoder rerank.

A third, structured "record lane" (`_record_lane`) adds the record-row chunks (call detail rows,
plate reads, evidence-log rows) that match the phones, plates, item tags and local time window a
query names (`hints.py`), plus the plates of vehicles the query describes by name (from the
case's vehicle entities). Those rows rank poorly by meaning, so when the query names an
identifier, or a time window together with calls or plate reads, they get reserved result slots
(fewer for a vehicle known only by description); evidence-log rows for a custody question only
join the rerank pool.

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
from datetime import UTC, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import psycopg
from psycopg import sql

from caselens_retrieval.encoders import (
    EMBEDDING_DIM,
    Embedder,
    Reranker,
    default_embedder,
    default_reranker,
)
from caselens_retrieval.hints import (
    CALL_TABLE,
    CUSTODY_TABLE,
    PLATE_TABLE,
    QueryHints,
    find_plates,
    owner_named_in,
    parse_hints,
    plate_of,
    vehicle_names_in,
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


@dataclass(frozen=True)
class RecordLane:
    """Record-row candidates for a query and how many result slots they may reserve.

    * "exact": the query names a phone / plate / item tag, or a time window with calls or
      plate reads: up to `lane_slots(top_k)` slots.
    * "described": plate reads of a vehicle the query describes ("where did the gray Civic
      go"): up to `described_slots(top_k)` slots.
    * "pool": evidence-log rows for a custody question: they only join the rerank pool.
    """

    hits: list[Candidate]
    mode: Literal["exact", "described", "pool"]
    hints: QueryHints

    def slots(self, top_k: int) -> int:
        if self.mode == "exact":
            return lane_slots(top_k)
        if self.mode == "described":
            return described_slots(top_k)
        return 0


_CASE_LATEST = "SELECT max(t_start) FROM public.chunks WHERE case_id = %(case_id)s"
_VEHICLES = (
    "SELECT canonical_name, normalized_key, attributes FROM public.entities "
    "WHERE case_id = %(case_id)s AND type = 'vehicle'"
)
_NON_ALNUM = re.compile(r"[^A-Z0-9]")


def _case_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _vehicle_plates(conn: Conn, case_id: uuid.UUID, query: str) -> tuple[list[str], list[str]]:
    """(plates of vehicles the query names by description, e.g. "the gray Civic"; full plates
    of plate-like tokens in the query resolved through entity aliases, e.g. a misread SYN482I)."""
    tokens = set(find_plates(query))
    derived: list[str] = []
    resolved: list[str] = []
    for name, key, attributes in conn.execute(_VEHICLES, {"case_id": case_id}).fetchall():
        attrs: dict[str, Any] = attributes if isinstance(attributes, dict) else {}
        plate = plate_of(attrs, key)
        if plate is None:
            continue
        raw_aliases = attrs.get("aliases")
        aliases = [str(a) for a in raw_aliases] if isinstance(raw_aliases, list) else []
        names = [str(name), *aliases]
        keys = {_NON_ALNUM.sub("", n.upper()) for n in [*names, str(key or "")]}
        if tokens & keys:
            resolved.append(plate)
        elif vehicle_names_in(query, names) or owner_named_in(
            query, str(attrs.get("description") or "")
        ):
            derived.append(plate)
    return list(dict.fromkeys(derived)), list(dict.fromkeys(resolved))


def _like(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _record_lane(
    conn: Conn,
    case_id: uuid.UUID,
    query: str,
    fts_query: str,
    filters: EvidenceFilters | None,
    settings: RetrievalSettings,
) -> RecordLane | None:
    """Record-row chunks matching the query's structured hints (inside the caller's
    transaction). None when the query has no usable hint."""
    if filters is not None and filters.modalities and "csv" not in filters.modalities:
        return None
    row = conn.execute(_CASE_LATEST, {"case_id": case_id}).fetchone()
    latest = row[0] if row else None
    year = latest.year if isinstance(latest, datetime) else datetime.now(UTC).year
    derived, resolved = _vehicle_plates(conn, case_id, query)
    hints = parse_hints(
        query, _case_timezone(settings.case_timezone), year, derived_plates=tuple(derived)
    )
    # A described vehicle narrows plate reads, not e.g. the evidence log of a search.
    use_derived = PLATE_TABLE in hints.tables or not hints.tables
    plates = list(dict.fromkeys([*hints.plates, *resolved, *(derived if use_derived else [])]))
    timed_tables = {CALL_TABLE, PLATE_TABLE} & set(hints.tables)
    mode: Literal["exact", "described", "pool"]
    if hints.has_identifiers or (hints.window and timed_tables):
        mode = "exact"
    elif derived and PLATE_TABLE in hints.tables:
        # A vehicle known only by description: its plate reads compete in the rerank pool
        # instead of taking reserved slots (reserving them crowded out narrative evidence).
        mode = "pool"
    elif CUSTODY_TABLE in hints.tables:
        mode = "pool"
    else:
        return None

    clauses, params = _filter_clauses(case_id, filters)
    clauses.append(sql.SQL("c.modality = 'csv'"))
    if hints.tables:
        clauses.append(sql.SQL("c.locator ->> 'table' = ANY(%(lane_tables)s::text[])"))
        params["lane_tables"] = list(hints.tables)
    if hints.phones:
        clauses.append(sql.SQL("c.text ILIKE ALL(%(lane_all)s::text[])"))
        params["lane_all"] = [_like(p) for p in hints.phones]
    any_of = [*plates, *hints.item_ids]
    if any_of:
        clauses.append(sql.SQL("c.text ILIKE ANY(%(lane_any)s::text[])"))
        params["lane_any"] = [_like(v) for v in any_of]
    if hints.window is not None and (hints.phones or timed_tables or plates):
        # Custody rows are dated by collection and their chain runs for days: no window.
        clauses.append(
            sql.SQL(
                "(c.locator ->> 'table' = %(lane_custody)s OR (c.t_start >= %(lane_from)s "
                "AND c.t_start <= %(lane_to)s))"
            )
        )
        params.update(
            lane_custody=CUSTODY_TABLE, lane_from=hints.window[0], lane_to=hints.window[1]
        )
    params["lane_fts"] = fts_query
    params["lane_n"] = settings.retrieval_candidates
    stmt = sql.SQL(
        "SELECT {cols} FROM public.chunks c WHERE {where} "
        "ORDER BY ts_rank_cd(c.tsv, websearch_to_tsquery('english', %(lane_fts)s)) DESC, "
        "c.t_start NULLS LAST, c.id LIMIT %(lane_n)s"
    ).format(cols=_COLUMNS, where=sql.SQL(" AND ").join(clauses))
    hits = [_candidate(r) for r in conn.execute(stmt, params).fetchall()]
    return RecordLane(hits, mode, hints) if hits else None


def lane_slots(top_k: int) -> int:
    """Result slots an exact record lane may take; the rest stay for the ranked results."""
    return max(top_k - max(2, top_k // 3), 1)


def described_slots(top_k: int) -> int:
    """Result slots for plate reads of a vehicle known only by its description."""
    return max((top_k + 1) // 2 - 1, 1)


def _with_reserved(
    ranked: Sequence[tuple[Candidate, float]], reserved: Sequence[uuid.UUID], top_k: int
) -> list[Scored]:
    """The best `top_k` of `ranked`, always including the `reserved` ids, in ranked order."""
    keep = set(reserved)
    for c, _ in ranked:
        if len(keep) >= top_k:
            break
        keep.add(c.chunk_id)
    return [Scored(c, score) for c, score in ranked if c.chunk_id in keep][:top_k]


def _retrieve(
    conn: Conn,
    case_id: uuid.UUID,
    query_vec: Sequence[float] | None,
    fts_query: str,
    filters: EvidenceFilters | None,
    settings: RetrievalSettings,
    query: str = "",
) -> tuple[list[Candidate], list[Candidate], RecordLane | None]:
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
        lane = (
            _record_lane(conn, case_id, query, fts_query, filters, settings)
            if settings.record_lane and query
            else None
        )
    return vector_hits, text_hits, lane


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
    vector_hits, text_hits, lane = _retrieve(
        conn, case_id, query_vec, fts_or_query(query), filters, s, query
    )
    t2 = time.perf_counter()

    lane_hits = lane.hits if lane is not None else []
    by_id = {c.chunk_id: c for c in (*vector_hits, *text_hits, *lane_hits)}
    lists = [[c.chunk_id for c in vector_hits], [c.chunk_id for c in text_hits]]
    if lane_hits:
        lists.append([c.chunk_id for c in lane_hits])
    fused = rrf_fuse(lists, k=s.rrf_k)
    lane_ids = [c.chunk_id for c in lane_hits[: s.rerank_candidates]]
    reserved = lane.slots(top_k) if lane is not None else 0
    ranker = (reranker or default_reranker()) if rerank else None
    if ranker is None or not fused:
        ranked = [(by_id[cid], score) for cid, score in fused]
        lane_order = lane_ids
    else:
        head = [cid for cid, _ in fused[: s.rerank_candidates]]
        pool = [by_id[cid] for cid in dict.fromkeys([*head, *lane_ids])]
        scores = ranker(query, [rerank_snippet(query, c.text, s.rerank_window_words) for c in pool])
        if len(scores) != len(pool):
            raise ValueError("reranker returned the wrong number of scores")
        order = sorted(range(len(pool)), key=lambda i: (-scores[i], i))
        ranked = [(pool[i], float(scores[i])) for i in order]
        in_lane = set(lane_ids)
        lane_order = [c.chunk_id for c, _ in ranked if c.chunk_id in in_lane]
    results = _with_reserved(ranked, lane_order[:reserved], top_k)
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
