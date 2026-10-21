from __future__ import annotations

import uuid
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import TupleRow

from caselens_workers.settings import WorkerSettings, libpq_url

Conn = psycopg.Connection[TupleRow]


def connect(url: str, role: str | None, user_id: uuid.UUID | None = None) -> Conn:
    """Open an autocommit connection that has switched to `role` (and app.user_id, for RLS).

    Callers group writes with `conn.transaction()`, which nests as savepoints, so the same code
    runs inside a test's rolled-back transaction.
    """
    conn = psycopg.connect(libpq_url(url), autocommit=True)
    if role is not None:
        conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
    if user_id is not None:
        conn.execute("SELECT set_config('app.user_id', %s, false)", (str(user_id),))
    return conn


def worker_connection(settings: WorkerSettings) -> Conn:
    url = settings.worker_database_url or settings.database_url
    if not url:
        raise RuntimeError("CASELENS_WORKER_DATABASE_URL (or CASELENS_DATABASE_URL) is not set")
    return connect(url, settings.worker_db_role)


def vector_literal(values: Any) -> str:
    """pgvector text form, e.g. '[0.1,0.2]', passed as a parameter and cast with ::vector."""
    return "[" + ",".join(f"{float(v):.7g}" for v in values) + "]"
