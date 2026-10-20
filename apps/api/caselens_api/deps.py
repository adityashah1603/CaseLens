"""Request dependencies: services on app.state, the authenticated DB session, case roles."""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Annotated, Any

from fastapi import Depends, Request

from caselens_api.auth import TokenVerifier, UserRow, resolve_user, verify_or_401
from caselens_api.common import parse_id, require_role
from caselens_api.db import Conn, Database, set_user
from caselens_api.problems import ProblemError
from caselens_api.settings import Settings
from caselens_api.storage import EvidenceStore

AskFn = Callable[..., Any]


class RateLimiter:
    """Sliding-window limit per key, in this process only (one uvicorn worker locally; the
    edge limiter covers multi-instance prod)."""

    def __init__(self, limit: int, window_s: float) -> None:
        self.limit = limit
        self.window_s = window_s
        self._hits: dict[uuid.UUID, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: uuid.UUID) -> int | None:
        """Record a hit and return None, or return the seconds to wait when over the limit."""
        now = time.monotonic()
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= now - self.window_s:
                hits.popleft()
            if len(hits) >= self.limit:
                oldest = hits[0] if hits else now
                return max(1, int(oldest + self.window_s - now) + 1)
            hits.append(now)
            return None


@dataclass
class Services:
    settings: Settings
    verifier: TokenVerifier
    db: Database | None
    store_factory: Callable[[], EvidenceStore]
    ask_limiter: RateLimiter
    # The agent entry point (`caselens_agent.runner.ask`); tests swap in a fake router.
    ask_kwargs: dict[str, Any] = field(default_factory=dict)
    _store: EvidenceStore | None = None

    def require_db(self) -> Database:
        if self.db is None:
            raise ProblemError(503, "The database is not configured (CASELENS_DATABASE_URL).")
        return self.db

    def store(self) -> EvidenceStore:
        if self._store is None:
            self._store = self.store_factory()
        return self._store


def get_services(request: Request) -> Services:
    services: Services = request.app.state.services
    return services


ServicesDep = Annotated[Services, Depends(get_services)]


def get_store(services: ServicesDep) -> EvidenceStore:
    return services.store()


StoreDep = Annotated[EvidenceStore, Depends(get_store)]


@dataclass
class Session:
    """One request's DB connection (caselens_app, app.user_id = user.id) and its user."""

    conn: Conn
    user: UserRow
    services: Services


def get_session(request: Request, services: ServicesDep) -> Iterator[Session]:
    identity = verify_or_401(services.verifier, request.headers.get("authorization"))
    db = services.require_db()
    with db.session(None) as conn:
        user = resolve_user(conn, identity)
        set_user(conn, user.id)
        yield Session(conn, user, services)


SessionDep = Annotated[Session, Depends(get_session)]


@dataclass
class CaseSession(Session):
    case_id: uuid.UUID
    role: str


def _case_session(minimum: str) -> Callable[[str, Session], CaseSession]:
    def dependency(case_id: str, session: SessionDep) -> CaseSession:
        cid = parse_id(case_id, "case")
        role = require_role(session.conn, cid, minimum)
        return CaseSession(session.conn, session.user, session.services, cid, role)

    return dependency


ViewerCase = Annotated[CaseSession, Depends(_case_session("viewer"))]
AnalystCase = Annotated[CaseSession, Depends(_case_session("analyst"))]
LeadCase = Annotated[CaseSession, Depends(_case_session("lead"))]


def get_admin(session: SessionDep) -> Session:
    if not session.user.is_admin:
        raise ProblemError(403, "This endpoint needs a platform admin.")
    return session


AdminSession = Annotated[Session, Depends(get_admin)]
