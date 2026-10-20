"""Entity resolution in three passes (guide: "Entity resolution"; design doc 4.5).

1. Exact: entities are keyed on a normalized identifier (E.164 phone, plate, or a normalized name),
   so the same key always reuses one entity row (`ensure_entity`).
2. Fuzzy: unlinked person / location / vehicle entities are compared with older canonical
   entities of the same type (rapidfuzz token-set ratio + pg_trgm similarity, name-aware for
   persons, attribute conflicts for vehicles). A score >= FUZZY_LINK links (method 'fuzzy').
3. LLM: scores in [ADJUDICATE_MIN, FUZZY_LINK) go to an adjudicator callback; a yes links
   (method 'llm').

Entities are never overwritten or deleted to merge them; merges are reversible `entity_links`.
Writers serialize per case with a transaction-scoped advisory lock so two files ingesting at
once cannot create the same entity twice.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from psycopg.types.json import Jsonb

from caselens_workers.db import Conn

EntityType = Literal["person", "vehicle", "phone", "location", "weapon"]

_NON_DIGITS = re.compile(r"\D")
_NON_ALNUM = re.compile(r"[^A-Z0-9]")
_NAME_PUNCT = re.compile(r"[^\w\s]")
_SPACES = re.compile(r"\s+")
_LEADING_ARTICLE = re.compile(r"^(?:the|a|an)\s+")

FUZZY_LINK = 0.92
ADJUDICATE_MIN = 0.75
PARTIAL_NAME_CAP = 0.9
LINKABLE_TYPES: tuple[EntityType, ...] = ("person", "location", "vehicle")


def normalize_phone(raw: str, default_country: str = "1") -> str | None:
    """'(360) 555-0142' / '360.555.0142' / '+1 360 555 0142' -> '+13605550142' (NANP)."""
    digits = _NON_DIGITS.sub("", raw)
    if len(digits) == 10:
        digits = default_country + digits
    if len(digits) == 11 and digits.startswith(default_country):
        return "+" + digits
    return None


def normalize_plate(raw: str) -> str | None:
    """'syn 4821' / 'SYN-4821' -> 'SYN4821'."""
    plate = _NON_ALNUM.sub("", raw.upper())
    return plate if 2 <= len(plate) <= 8 else None


def normalize_name(raw: str) -> str | None:
    """'The  Riverside Lot.' -> 'riverside lot'; 'T. Kemper' -> 't kemper'; '&' -> 'and'."""
    text = raw.casefold().replace("&", " and ")
    text = _NAME_PUNCT.sub(" ", text)
    text = _LEADING_ARTICLE.sub("", _SPACES.sub(" ", text).strip())
    return text or None


def lock_case(conn: Conn, case_id: uuid.UUID) -> None:
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"entities:{case_id}",))


def ensure_entity(
    conn: Conn,
    case_id: uuid.UUID,
    entity_type: EntityType,
    canonical_name: str,
    normalized_key: str | None,
    attributes: dict[str, Any] | None = None,
) -> uuid.UUID:
    """Return the entity with this (type, normalized_key) in the case, creating it if needed.

    Call inside a transaction; takes the per-case lock. Entities without a key are always new
    (fuzzy / LLM passes link them later).
    """
    lock_case(conn, case_id)
    if normalized_key is not None:
        row = conn.execute(
            "SELECT id FROM public.entities WHERE case_id = %s AND type = %s "
            "AND normalized_key = %s ORDER BY created_at, id LIMIT 1",
            (case_id, entity_type, normalized_key),
        ).fetchone()
        if row is not None:
            existing: uuid.UUID = row[0]
            return existing
    entity_id = uuid.uuid4()
    # clock_timestamp(), not now(): entities created in one transaction keep their creation
    # order, which decides link direction (newer -> older canonical).
    conn.execute(
        "INSERT INTO public.entities (id, case_id, type, canonical_name, normalized_key, "
        "attributes, created_at) VALUES (%s, %s, %s, %s, %s, %s, clock_timestamp())",
        (entity_id, case_id, entity_type, canonical_name, normalized_key, Jsonb(attributes or {})),
    )
    return entity_id


def add_mention(
    conn: Conn,
    case_id: uuid.UUID,
    entity_id: uuid.UUID,
    chunk_id: uuid.UUID,
    surface_text: str,
    confidence: float,
    review_threshold: float = 0.6,
) -> None:
    conn.execute(
        "INSERT INTO public.mentions (case_id, entity_id, chunk_id, surface_text, confidence, "
        "needs_review) VALUES (%s, %s, %s, %s, %s, %s)",
        (case_id, entity_id, chunk_id, surface_text, confidence, confidence < review_threshold),
    )


def merge_attributes(conn: Conn, entity_id: uuid.UUID, attributes: dict[str, Any]) -> None:
    """Fill attribute keys the entity does not have yet and union `aliases`. Never overwrites."""
    row = conn.execute(
        "SELECT attributes FROM public.entities WHERE id = %s", (entity_id,)
    ).fetchone()
    if row is None:
        return
    current: dict[str, Any] = dict(row[0] or {})
    merged = dict(current)
    for key, value in attributes.items():
        if value is None or value == "" or value == []:
            continue
        if key == "aliases":
            seen = list(merged.get("aliases") or [])
            merged["aliases"] = seen + [a for a in value if a not in seen]
        elif key not in merged:
            merged[key] = value
    if merged != current:
        conn.execute(
            "UPDATE public.entities SET attributes = %s WHERE id = %s", (Jsonb(merged), entity_id)
        )


@dataclass(frozen=True)
class Candidate:
    id: uuid.UUID
    type: EntityType
    name: str
    attributes: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True)
class Verdict:
    same: bool
    confidence: float


# (newer entity, older canonical entity) -> verdict, or None when no adjudicator is available.
Adjudicator = Callable[[Candidate, Candidate], Verdict | None]


@dataclass
class LinkStats:
    fuzzy: int = 0
    llm: int = 0
    adjudicated: int = 0
    adjudication_skipped: int = 0
    pairs: list[tuple[uuid.UUID, uuid.UUID, str]] = field(default_factory=list)


def _person_compatible(a: str, b: str) -> bool:
    """Same surname, and given names equal or one is the other's initial."""
    ta, tb = a.split(), b.split()
    if len(ta) < 2 or len(tb) < 2 or ta[-1] != tb[-1]:
        return False
    ga, gb = ta[0], tb[0]
    return ga == gb or (len(ga) == 1 and gb.startswith(ga)) or (len(gb) == 1 and ga.startswith(gb))


def _vehicle_conflict(a: dict[str, Any], b: dict[str, Any]) -> bool:
    for key in ("plate", "color", "make"):
        va, vb = a.get(key), b.get(key)
        if va and vb and normalize_name(str(va)) != normalize_name(str(vb)):
            return True
    return False


def name_score(entity_type: EntityType, a: str, b: str, trigram: float = 0.0) -> float:
    """Combined similarity in 0..1 of two normalized names (rapidfuzz + pg_trgm)."""
    from rapidfuzz import fuzz

    score = max(fuzz.token_set_ratio(a, b) / 100.0, trigram)
    if entity_type == "person" and _person_compatible(a, b):
        score = max(score, 0.95)
    return score


def _candidate(row: tuple[Any, ...]) -> Candidate:
    return Candidate(row[0], row[1], row[2], dict(row[3] or {}), row[4])


def link_similar_entities(
    conn: Conn,
    case_id: uuid.UUID,
    entity_ids: Iterable[uuid.UUID],
    adjudicate: Adjudicator | None = None,
) -> LinkStats:
    """Passes 2 and 3 for the given entities (those touched by one evidence file).

    An entity is considered only while it has no active outgoing link. It is compared with older
    entities of the same type that are themselves canonical (no active outgoing link), and the
    link always goes newer -> older. Pairs that already have any link row, active or reverted,
    are never linked again, so an analyst's revert sticks. Only INSERTs into entity_links.
    If two or more canonical entities score above FUZZY_LINK, the best one is adjudicated
    instead of auto-linked ("Hale" next to two different Hales).
    """
    lock_case(conn, case_id)
    stats = LinkStats()
    rows = conn.execute(
        "SELECT id, type, canonical_name, attributes, created_at FROM public.entities "
        "WHERE case_id = %s AND id = ANY(%s) AND type = ANY(%s) ORDER BY created_at, id",
        (case_id, list(set(entity_ids)), list(LINKABLE_TYPES)),
    ).fetchall()
    for row in rows:
        entity = _candidate(row)
        name = normalize_name(entity.name)
        if name is None or _has_active_outgoing(conn, entity.id):
            continue
        scored = _score_older_roots(conn, case_id, entity, name)
        if not scored:
            continue
        best_score, best = scored[0]
        crowded = len(scored) > 1 and scored[1][0] >= FUZZY_LINK
        if best_score >= FUZZY_LINK and not crowded:
            _insert_link(conn, case_id, entity.id, best.id, "fuzzy", best_score)
            stats.fuzzy += 1
            stats.pairs.append((entity.id, best.id, "fuzzy"))
        elif best_score >= ADJUDICATE_MIN:
            verdict = adjudicate(entity, best) if adjudicate is not None else None
            if verdict is None:
                stats.adjudication_skipped += 1
                continue
            stats.adjudicated += 1
            if verdict.same:
                _insert_link(conn, case_id, entity.id, best.id, "llm", verdict.confidence)
                stats.llm += 1
                stats.pairs.append((entity.id, best.id, "llm"))
    return stats


def _has_active_outgoing(conn: Conn, entity_id: uuid.UUID) -> bool:
    row = conn.execute(
        "SELECT 1 FROM public.entity_links WHERE from_entity_id = %s AND status = 'active' LIMIT 1",
        (entity_id,),
    ).fetchone()
    return row is not None


def _score_older_roots(
    conn: Conn, case_id: uuid.UUID, entity: Candidate, name: str
) -> list[tuple[float, Candidate]]:
    rows = conn.execute(
        "SELECT o.id, o.type, o.canonical_name, o.attributes, o.created_at, "
        "similarity(lower(o.canonical_name), %s) "
        "FROM public.entities o "
        "WHERE o.case_id = %s AND o.type = %s AND (o.created_at, o.id) < (%s, %s) "
        "AND NOT EXISTS (SELECT 1 FROM public.entity_links l WHERE l.from_entity_id = o.id "
        "AND l.status = 'active') "
        "AND NOT EXISTS (SELECT 1 FROM public.entity_links l WHERE "
        "(l.from_entity_id = %s AND l.to_entity_id = o.id) OR "
        "(l.from_entity_id = o.id AND l.to_entity_id = %s))",
        (name, case_id, entity.type, entity.created_at, entity.id, entity.id, entity.id),
    ).fetchall()
    scored: list[tuple[float, Candidate]] = []
    for r in rows:
        other = _candidate(r[:5])
        other_name = normalize_name(other.name)
        if other_name is None:
            continue
        if entity.type == "vehicle" and _vehicle_conflict(entity.attributes, other.attributes):
            continue
        score = name_score(entity.type, name, other_name, float(r[5]))
        if entity.type == "person" and len(other_name.split()) == 1 < len(name.split()):
            # A bare older surname ("Hale") is a weak canonical for a full name: adjudicate.
            score = min(score, PARTIAL_NAME_CAP)
        scored.append((score, other))
    scored.sort(key=lambda item: (-item[0], item[1].created_at, str(item[1].id)))
    return scored


def _insert_link(
    conn: Conn,
    case_id: uuid.UUID,
    from_id: uuid.UUID,
    to_id: uuid.UUID,
    method: Literal["fuzzy", "llm"],
    confidence: float,
) -> None:
    conn.execute(
        "INSERT INTO public.entity_links (case_id, from_entity_id, to_entity_id, method, "
        "confidence) VALUES (%s, %s, %s, %s, %s)",
        (case_id, from_id, to_id, method, round(min(max(confidence, 0.0), 1.0), 4)),
    )
