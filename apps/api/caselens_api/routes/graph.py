"""Timeline, entities (list, detail, merge revert), the case graph, and conflicts."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query

from caselens_api import models as m
from caselens_api.common import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    cursor_params,
    decode_cursor,
    encode_cursor,
    parse_id,
    require_role,
    write_audit,
)
from caselens_api.db import Conn
from caselens_api.deps import ViewerCase
from caselens_api.problems import conflict, not_found

router = APIRouter(prefix="/v1/cases/{case_id}")

Cursor = Annotated[str | None, Query(max_length=512)]
Limit = Annotated[int, Query(ge=1, le=MAX_LIMIT)]
TRIGRAM_THRESHOLD = 0.3
MENTION_LIMIT = 1000


def _float(value: Any) -> float | None:
    return None if value is None else float(value)


# ---- timeline ------------------------------------------------------------------------------

# The entity filter covers the entity plus its actively linked entities (one hop), like the
# agent's get_timeline tool: an event involves them when its location is one of them or one of
# its source chunks mentions one of them.
_EVENTS = """
WITH ents AS (
    SELECT %(entity_id)s::uuid AS id WHERE %(entity_id)s::uuid IS NOT NULL
    UNION
    SELECT CASE WHEN l.from_entity_id = %(entity_id)s::uuid THEN l.to_entity_id
                ELSE l.from_entity_id END
      FROM public.entity_links AS l
     WHERE l.case_id = %(case_id)s AND l.status = 'active'
       AND (l.from_entity_id = %(entity_id)s::uuid OR l.to_entity_id = %(entity_id)s::uuid)
)
SELECT ev.id, ev.t_start, ev.t_end, ev.location_id, loc.canonical_name, ev.description,
       ev.confidence, ev.needs_review, ev.source_chunk_ids
  FROM public.events AS ev
  LEFT JOIN public.entities AS loc ON loc.id = ev.location_id AND loc.case_id = ev.case_id
 WHERE ev.case_id = %(case_id)s
   AND (%(time_from)s::timestamptz IS NULL OR ev.t_start >= %(time_from)s::timestamptz)
   AND (%(time_to)s::timestamptz IS NULL OR ev.t_start <= %(time_to)s::timestamptz)
   AND (%(entity_id)s::uuid IS NULL
        OR ev.location_id IN (SELECT id FROM ents)
        OR EXISTS (SELECT 1 FROM public.mentions AS mn
                    WHERE mn.case_id = ev.case_id
                      AND mn.chunk_id = ANY(ev.source_chunk_ids)
                      AND mn.entity_id IN (SELECT id FROM ents)))
   AND (%(c_ts)s::timestamptz IS NULL OR (ev.t_start, ev.id) > (%(c_ts)s, %(c_id)s::uuid))
 ORDER BY ev.t_start, ev.id
 LIMIT %(limit)s
"""
_CHUNK_SOURCES = """
SELECT c.id, c.evidence_id, c.locator
  FROM public.chunks AS c
 WHERE c.case_id = %(case_id)s AND c.id = ANY(%(ids)s::uuid[])
"""


@router.get("/timeline", response_model=m.TimelineEventList)
def get_timeline(
    case: ViewerCase,
    entity_id: uuid.UUID | None = None,
    time_from: Annotated[datetime | None, Query(alias="from")] = None,
    time_to: Annotated[datetime | None, Query(alias="to")] = None,
    cursor: Cursor = None,
    limit: Limit = DEFAULT_LIMIT,
) -> m.TimelineEventList:
    conn = case.conn
    after = decode_cursor(cursor, (datetime, uuid.UUID))
    rows = conn.execute(
        _EVENTS,
        {
            "case_id": case.case_id,
            "entity_id": entity_id,
            "time_from": time_from,
            "time_to": time_to,
            **cursor_params(after, ("c_ts", "c_id")),
            "limit": limit + 1,
        },
    ).fetchall()
    page = rows[:limit]
    chunk_ids = sorted({cid for r in page for cid in (r[8] or [])}, key=str)
    chunks: dict[uuid.UUID, tuple[uuid.UUID, dict[str, Any]]] = {}
    if chunk_ids:
        for c in conn.execute(
            _CHUNK_SOURCES, {"case_id": case.case_id, "ids": chunk_ids}
        ).fetchall():
            chunks[c[0]] = (c[1], c[2])
    items = [
        m.TimelineEvent(
            id=r[0],
            t_start=r[1],
            t_end=r[2],
            location=None if r[3] is None else m.EventLocation(entity_id=r[3], name=r[4]),
            description=r[5],
            confidence=_float(r[6]),
            needs_review=r[7],
            sources=[
                m.TimelineSource(chunk_id=cid, evidence_id=chunks[cid][0], locator=chunks[cid][1])
                for cid in (r[8] or [])
                if cid in chunks
            ],
        )
        for r in page
    ]
    with conn.transaction():
        write_audit(
            conn,
            case.user.id,
            case.case_id,
            "timeline.view",
            {
                "entity_id": None if entity_id is None else str(entity_id),
                "from": None if time_from is None else time_from.isoformat(),
                "to": None if time_to is None else time_to.isoformat(),
                "returned": len(items),
            },
        )
    nxt = encode_cursor(rows[limit - 1][1], rows[limit - 1][0]) if len(rows) > limit else None
    return m.TimelineEventList(items=items, next_cursor=nxt)


# ---- entities ------------------------------------------------------------------------------

_ENTITY_COLUMNS = """
SELECT e.id, e.case_id, e.type, e.canonical_name, e.normalized_key, e.attributes,
       (SELECT count(*) FROM public.mentions AS mn
         WHERE mn.case_id = e.case_id AND mn.entity_id = e.id),
       e.created_at
"""
_ENTITY_PAGE = (
    _ENTITY_COLUMNS
    + """
  FROM public.entities AS e
 WHERE e.case_id = %(case_id)s
   AND (%(type)s::text IS NULL OR e.type = %(type)s::text)
   AND (%(c_name)s::text IS NULL OR (e.canonical_name, e.id) > (%(c_name)s, %(c_id)s::uuid))
 ORDER BY e.canonical_name, e.id
 LIMIT %(limit)s
"""
)
# Fuzzy search: trigram similarity of the whole name, or of the best-matching word span
# (so "Kemper" finds "Travis Kemper"); substring matches always qualify. Ranked by score.
_ENTITY_SEARCH = (
    _ENTITY_COLUMNS
    + """, s.score
  FROM public.entities AS e
 CROSS JOIN LATERAL (
     SELECT GREATEST(similarity(e.canonical_name, %(q)s),
                     word_similarity(%(q)s, e.canonical_name),
                     CASE WHEN e.canonical_name ILIKE %(pattern)s THEN %(threshold)s ELSE 0 END
            )::float8 AS score
 ) AS s
 WHERE e.case_id = %(case_id)s
   AND (%(type)s::text IS NULL OR e.type = %(type)s::text)
   AND s.score >= %(threshold)s
   AND (%(c_score)s::float8 IS NULL OR s.score < %(c_score)s::float8
        OR (s.score = %(c_score)s::float8 AND e.id > %(c_id)s::uuid))
 ORDER BY s.score DESC, e.id
 LIMIT %(limit)s
"""
)
_ENTITY_ONE = (
    _ENTITY_COLUMNS
    + """
  FROM public.entities AS e
 WHERE e.case_id = %(case_id)s AND e.id = %(entity_id)s
"""
)
_ENTITY_MENTIONS = """
SELECT mn.id, mn.entity_id, mn.chunk_id, c.evidence_id, mn.surface_text, mn.confidence,
       mn.needs_review, c.locator, mn.created_at
  FROM public.mentions AS mn
  JOIN public.chunks AS c ON c.id = mn.chunk_id AND c.case_id = mn.case_id
 WHERE mn.case_id = %(case_id)s AND mn.entity_id = %(entity_id)s
 ORDER BY mn.created_at, mn.id
 LIMIT %(limit)s
"""
_LINK_COLUMNS = """
SELECT l.id, l.case_id, l.from_entity_id, l.to_entity_id, l.method, l.confidence, l.status,
       l.created_by, l.created_at, l.reverted_at, l.reverted_by
  FROM public.entity_links AS l
"""
_ENTITY_LINKS = (
    _LINK_COLUMNS
    + """
 WHERE l.case_id = %(case_id)s
   AND (l.from_entity_id = %(entity_id)s OR l.to_entity_id = %(entity_id)s)
 ORDER BY l.created_at, l.id
"""
)
_LINKED_NAMES = """
SELECT e.canonical_name
  FROM public.entity_links AS l
  JOIN public.entities AS e
    ON e.case_id = l.case_id
   AND e.id = CASE WHEN l.from_entity_id = %(entity_id)s THEN l.to_entity_id
                   ELSE l.from_entity_id END
 WHERE l.case_id = %(case_id)s AND l.status = 'active'
   AND (l.from_entity_id = %(entity_id)s OR l.to_entity_id = %(entity_id)s)
 ORDER BY e.created_at, e.id
"""


def _entity(row: tuple[Any, ...]) -> m.Entity:
    return m.Entity(
        id=row[0],
        case_id=row[1],
        type=row[2],
        canonical_name=row[3],
        normalized_key=row[4],
        attributes=row[5] if isinstance(row[5], dict) else {},
        mention_count=row[6],
        created_at=row[7],
    )


def _link(row: tuple[Any, ...]) -> m.EntityLink:
    return m.EntityLink(
        id=row[0],
        case_id=row[1],
        from_entity_id=row[2],
        to_entity_id=row[3],
        method=row[4],
        confidence=_float(row[5]),
        status=row[6],
        created_by=row[7],
        created_at=row[8],
        reverted_at=row[9],
        reverted_by=row[10],
    )


def _like_pattern(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


@router.get("/entities", response_model=m.EntityList)
def list_entities(
    case: ViewerCase,
    type: m.EntityType | None = None,
    q: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    cursor: Cursor = None,
    limit: Limit = DEFAULT_LIMIT,
) -> m.EntityList:
    base = {"case_id": case.case_id, "type": type, "limit": limit + 1}
    if q is None:
        after = decode_cursor(cursor, (str, uuid.UUID))
        rows = case.conn.execute(
            _ENTITY_PAGE, {**base, **cursor_params(after, ("c_name", "c_id"))}
        ).fetchall()
        last = rows[limit - 1] if len(rows) > limit else None
        nxt = encode_cursor(last[3], last[0]) if last is not None else None
    else:
        after = decode_cursor(cursor, (float, uuid.UUID))
        rows = case.conn.execute(
            _ENTITY_SEARCH,
            {
                **base,
                "q": q,
                "pattern": _like_pattern(q),
                "threshold": TRIGRAM_THRESHOLD,
                **cursor_params(after, ("c_score", "c_id")),
            },
        ).fetchall()
        last = rows[limit - 1] if len(rows) > limit else None
        nxt = encode_cursor(repr(float(last[8])), last[0]) if last is not None else None
    return m.EntityList(items=[_entity(r) for r in rows[:limit]], next_cursor=nxt)


def _aliases(conn: Conn, entity: m.Entity, mentions: list[m.Mention]) -> list[str]:
    raw = entity.attributes.get("aliases")
    names = [str(a) for a in raw] if isinstance(raw, list) else []
    params = {"case_id": entity.case_id, "entity_id": entity.id}
    names += [r[0] for r in conn.execute(_LINKED_NAMES, params).fetchall()]
    names += [mn.surface_text for mn in mentions]
    seen = {entity.canonical_name.casefold()}
    out = []
    for name in names:
        key = name.strip().casefold()
        if key and key not in seen:
            seen.add(key)
            out.append(name.strip())
    return out


@router.get("/entities/{entity_id}", response_model=m.EntityDetail)
def get_entity(entity_id: str, case: ViewerCase) -> m.EntityDetail:
    eid = parse_id(entity_id, "entity")
    params = {"case_id": case.case_id, "entity_id": eid}
    row = case.conn.execute(_ENTITY_ONE, params).fetchone()
    if row is None:
        raise not_found("No such entity.")
    entity = _entity(row)
    mentions = [
        m.Mention(
            id=r[0],
            entity_id=r[1],
            chunk_id=r[2],
            evidence_id=r[3],
            surface_text=r[4],
            confidence=_float(r[5]),
            needs_review=r[6],
            locator=r[7],
            created_at=r[8],
        )
        for r in case.conn.execute(_ENTITY_MENTIONS, {**params, "limit": MENTION_LIMIT}).fetchall()
    ]
    links = [_link(r) for r in case.conn.execute(_ENTITY_LINKS, params).fetchall()]
    return m.EntityDetail(
        **entity.model_dump(),
        aliases=_aliases(case.conn, entity, mentions),
        mentions=mentions,
        links=links,
    )


_LINK_ONE = _LINK_COLUMNS + " WHERE l.case_id = %(case_id)s AND l.id = %(link_id)s"
_REVERT = """
UPDATE public.entity_links
   SET status = 'reverted', reverted_at = now(), reverted_by = %(user_id)s
 WHERE case_id = %(case_id)s AND id = %(link_id)s AND status = 'active'
"""


@router.post("/entity-links/{link_id}/revert", response_model=m.EntityLink)
def revert_entity_link(link_id: str, case: ViewerCase) -> m.EntityLink:
    require_role(case.conn, case.case_id, "analyst")
    lid = parse_id(link_id, "entity link")
    params = {"case_id": case.case_id, "link_id": lid, "user_id": case.user.id}
    conn = case.conn
    with conn.transaction():
        row = conn.execute(_LINK_ONE, params).fetchone()
        if row is None:
            raise not_found("No such entity link.")
        if row[6] == "reverted":
            raise conflict("This link is already reverted.", "already-reverted")
        cur = conn.execute(_REVERT, params)
        if cur.rowcount != 1:
            raise conflict("This link is already reverted.", "already-reverted")
        write_audit(
            conn,
            case.user.id,
            case.case_id,
            "entity_link.revert",
            {
                "link_id": str(lid),
                "from_entity_id": str(row[2]),
                "to_entity_id": str(row[3]),
                "method": row[4],
            },
        )
    reverted = conn.execute(_LINK_ONE, params).fetchone()
    if reverted is None:
        raise not_found("No such entity link.")
    return _link(reverted)


# ---- graph ---------------------------------------------------------------------------------

_GRAPH_NODES = """
SELECT e.id, e.type, e.canonical_name,
       (SELECT count(*) FROM public.mentions AS mn
         WHERE mn.case_id = e.case_id AND mn.entity_id = e.id)
  FROM public.entities AS e
 WHERE e.case_id = %(case_id)s
   AND (%(types)s::text[] IS NULL OR e.type = ANY(%(types)s::text[]))
 ORDER BY e.type, e.canonical_name, e.id
"""
_GRAPH_SAME_AS = """
SELECT l.id, l.from_entity_id, l.to_entity_id, l.confidence
  FROM public.entity_links AS l
 WHERE l.case_id = %(case_id)s AND l.status = 'active'
 ORDER BY l.created_at, l.id
"""
_GRAPH_CO_MENTION = """
SELECT a.entity_id, b.entity_id, count(DISTINCT a.chunk_id)
  FROM public.mentions AS a
  JOIN public.mentions AS b
    ON b.case_id = a.case_id AND b.chunk_id = a.chunk_id AND a.entity_id < b.entity_id
 WHERE a.case_id = %(case_id)s
 GROUP BY a.entity_id, b.entity_id
 ORDER BY a.entity_id, b.entity_id
"""


@router.get("/graph", response_model=m.Graph)
def get_graph(
    case: ViewerCase,
    type: Annotated[list[m.EntityType] | None, Query()] = None,
) -> m.Graph:
    params = {"case_id": case.case_id, "types": list(type) if type else None}
    nodes = [
        m.GraphNode(id=r[0], type=r[1], label=r[2], mention_count=r[3])
        for r in case.conn.execute(_GRAPH_NODES, params).fetchall()
    ]
    ids = {n.id for n in nodes}
    edges = [
        m.GraphEdge(
            id=str(r[0]),
            source=r[1],
            target=r[2],
            kind="same_as",
            weight=float(r[3]) if r[3] is not None else 1.0,
            link_id=r[0],
        )
        for r in case.conn.execute(_GRAPH_SAME_AS, params).fetchall()
        if r[1] in ids and r[2] in ids
    ]
    edges += [
        m.GraphEdge(
            id=f"co_mention:{r[0]}:{r[1]}",
            source=r[0],
            target=r[1],
            kind="co_mention",
            weight=float(r[2]),
            link_id=None,
        )
        for r in case.conn.execute(_GRAPH_CO_MENTION, params).fetchall()
        if r[0] in ids and r[1] in ids
    ]
    return m.Graph(nodes=nodes, edges=edges)


# ---- conflicts -----------------------------------------------------------------------------

_CONFLICT_PAGE = """
SELECT cf.id, cf.case_id, cf.agent_run_id, cf.description, cf.claim_a, cf.claim_b, cf.status,
       cf.created_at
  FROM public.conflicts AS cf
 WHERE cf.case_id = %(case_id)s
   AND (%(status)s::text IS NULL OR cf.status = %(status)s::text)
   AND (%(c_ts)s::timestamptz IS NULL OR (cf.created_at, cf.id) < (%(c_ts)s, %(c_id)s::uuid))
 ORDER BY cf.created_at DESC, cf.id DESC
 LIMIT %(limit)s
"""


@router.get("/conflicts", response_model=m.ConflictList)
def list_conflicts(
    case: ViewerCase,
    status: m.ConflictStatus | None = None,
    cursor: Cursor = None,
    limit: Limit = DEFAULT_LIMIT,
) -> m.ConflictList:
    after = decode_cursor(cursor, (datetime, uuid.UUID))
    rows = case.conn.execute(
        _CONFLICT_PAGE,
        {
            "case_id": case.case_id,
            "status": status,
            **cursor_params(after, ("c_ts", "c_id")),
            "limit": limit + 1,
        },
    ).fetchall()
    items = [
        m.ConflictRecord(
            id=r[0],
            case_id=r[1],
            agent_run_id=r[2],
            description=r[3],
            claim_a=m.ConflictClaim.model_validate(r[4]),
            claim_b=m.ConflictClaim.model_validate(r[5]),
            status=r[6],
            created_at=r[7],
        )
        for r in rows[:limit]
    ]
    nxt = encode_cursor(rows[limit - 1][7], rows[limit - 1][0]) if len(rows) > limit else None
    return m.ConflictList(items=items, next_cursor=nxt)
