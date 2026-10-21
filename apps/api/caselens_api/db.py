"""Postgres access for the request path.

One psycopg pool on the configured login. Every checkout switches to a group role
(`caselens_app` for requests, `caselens_readonly` for semantic-layer SQL) and sets
`app.user_id`, so RLS applies to every statement; both are reset before the connection goes back
to the pool, and the pool's reset hook does it again as a safety net (a connection that cannot be
reset is discarded). Connections are autocommit; writes are grouped with `conn.transaction()`.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import psycopg
from psycopg import sql
from psycopg.rows import TupleRow
from psycopg_pool import ConnectionPool

from caselens_workers.settings import libpq_url

Conn = psycopg.Connection[TupleRow]
log = logging.getLogger(__name__)

_LOGIN = (
    "SELECT r.rolname, r.rolsuper, r.rolbypassrls FROM pg_catalog.pg_roles AS r "
    "WHERE r.rolname = session_user"
)


@dataclass(frozen=True)
class LoginInfo:
    name: str
    superuser: bool
    bypass_rls: bool

    @property
    def unsafe(self) -> bool:
        return self.superuser or self.bypass_rls


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

    def login_info(self, timeout: float = 30.0) -> LoginInfo:
        """Attributes of the role this pool logs in as (session_user, not the SET ROLE one)."""
        with self.pool.connection(timeout=timeout) as conn:
            row = conn.execute(_LOGIN).fetchone()
        if row is None:
            raise RuntimeError("the database login role is not visible in pg_roles")
        return LoginInfo(str(row[0]), bool(row[1]), bool(row[2]))

    def check_login(self, *, strict: bool, timeout: float = 30.0) -> LoginInfo | None:
        """Refuse (strict) or warn (local) when the pool's login is superuser or BYPASSRLS.

        Strict mode also refuses when the login cannot be checked at all."""
        try:
            info = self.login_info(timeout)
        except Exception as exc:
            if strict:
                raise RuntimeError(f"could not verify the database login role: {exc}") from exc
            log.warning("could not verify the database login role: %s", exc)
            return None
        if info.unsafe:
            message = (
                f"the API database login {info.name!r} is "
                f"{'a superuser' if info.superuser else 'BYPASSRLS'}; use a dedicated "
                "NOSUPERUSER NOBYPASSRLS login (CASELENS_API_DATABASE_URL, see apps/api/README.md)"
            )
            if strict:
                raise RuntimeError(message)
            log.warning("%s (allowed only because CASELENS_ENVIRONMENT=local)", message)
        return info

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
