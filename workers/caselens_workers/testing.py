"""Test helpers shared by worker tests (SYNTHETIC data only)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from psycopg import sql

from caselens_workers.db import Conn
from caselens_workers.storage import MemoryObjectStore


def as_role(conn: Conn, role: str | None, user_id: uuid.UUID | None = None) -> None:
    conn.execute("RESET ROLE")
    conn.execute(
        "SELECT set_config('app.user_id', %s, true)", ("" if user_id is None else str(user_id),)
    )
    if role is not None:
        conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))


@dataclass
class SeededEvidence:
    case_id: uuid.UUID
    evidence_id: uuid.UUID
    user_id: uuid.UUID
    store: MemoryObjectStore
