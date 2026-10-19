"""Entity resolution. Pass 1 (here): exact match on normalized identifiers (E.164 phone, plate).

Entities are never overwritten or deleted to merge them; merges are reversible `entity_links`.
Writers serialize per case with a transaction-scoped advisory lock so two files ingesting at
once cannot create the same entity twice.
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Literal

from psycopg.types.json import Jsonb

from caselens_workers.db import Conn

EntityType = Literal["person", "vehicle", "phone", "location", "weapon"]

_NON_DIGITS = re.compile(r"\D")
_NON_ALNUM = re.compile(r"[^A-Z0-9]")


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
    conn.execute(
        "INSERT INTO public.entities (id, case_id, type, canonical_name, normalized_key, "
        "attributes) VALUES (%s, %s, %s, %s, %s, %s)",
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
