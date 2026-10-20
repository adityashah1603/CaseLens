"""Postgres access for the request path.

One psycopg pool on the configured login. Every checkout switches to a group role
(`caselens_app` for requests, `caselens_readonly` for semantic-layer SQL) and sets
`app.user_id`, so RLS applies to every statement; both are reset before the connection goes back
to the pool, and the pool's reset hook does it again as a safety net (a connection that cannot be
reset is discarded). Connections are autocommit; writes are grouped with `conn.transaction()`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg import sql
from psycopg.rows import TupleRow
from psycopg_pool import ConnectionPool

from caselens_workers.settings import libpq_url

Conn = psycopg.Connection[TupleRow]


def set_user(conn: Conn, user_id: uuid.UUID | None) -> None:
    """Bind the RLS identity for the rest of this checkout (session level, reset on return)."""
    conn.execute(
        "SELECT set_config('app.user_id', %s, false)", ("" if user_id is None else str(user_id),)
    )


def _reset(conn: Conn) -> None:
    if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        conn.rollback()
    conn.execute("RESET ROLE")
    conn.execute("SELECT set_config('app.user_id', '', false)")


class Database:
    def __init__(
        self,
        url: str,
        app_role: str,
        readonly_role: str,
        min_size: int = 1,
        max_size: int = 8,
    ) -> None:
        self.app_role = app_role
        self.readonly_role = readonly_role
        self.pool: ConnectionPool[Conn] = ConnectionPool(
            libpq_url(url),
            kwargs={"autocommit": True},
            min_size=min_size,
            max_size=max_size,
            open=False,
            reset=_reset,
            check=ConnectionPool.check_connection,
            name="caselens-api",
        )

    def open(self) -> None:
        self.pool.open(wait=False)

    def close(self) -> None:
        self.pool.close()

    @contextmanager
    def session(self, user_id: uuid.UUID | None, *, readonly: bool = False) -> Iterator[Conn]:
        role = self.readonly_role if readonly else self.app_role
        with self.pool.connection() as conn:
            conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
            set_user(conn, user_id)
            try:
                yield conn
            finally:
                _reset(conn)
