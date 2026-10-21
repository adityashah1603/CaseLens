"""Request dependencies: services on app.state, the authenticated DB session, case roles."""

from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable, Hashable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Annotated, Any

from fastapi import Depends, Request

from caselens_api.auth import (
    TokenVerifier,
    UserRow,
    resolve_demo_viewer,
    resolve_user,
    verify_or_401,
)
from caselens_api.common import parse_id, require_role
from caselens_api.db import Conn, Database, set_user
from caselens_api.problems import ProblemError, unauthorized
from caselens_api.settings import Settings
from caselens_api.storage import EvidenceStore

AskFn = Callable[..., Any]
MAX_LIMITER_KEYS = 10_000
MAX_VISITORS = 10_000
MAX_RUNS_PER_VISITOR = 200


def client_ip(request: Request, trusted_proxy_hops: int) -> str:
    """The caller's address: the socket peer, or with N trusted proxies the N-th
    X-Forwarded-For entry from the right (entries further left are client-controlled)."""
    peer = request.client.host if request.client else "unknown"
    if trusted_proxy_hops <= 0:
        return peer
    forwarded = [
        part.strip()
        for header in request.headers.getlist("x-forwarded-for")
        for part in header.split(",")
        if part.strip()
    ]
    if len(forwarded) < trusted_proxy_hops:
        return peer
    return forwarded[-trusted_proxy_hops]


class RateLimiter:
    """Sliding-window limit per key, in this process only (one uvicorn worker locally; the
    edge limiter covers multi-instance prod)."""

    def __init__(self, limit: int, window_s: float) -> None:
        self.limit = limit
        self.window_s = window_s
        self._hits: dict[Hashable, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: Hashable) -> int | None:
        """Record a hit and return None, or return the seconds to wait when over the limit."""
        now = time.monotonic()
        with self._lock:
            if len(self._hits) >= MAX_LIMITER_KEYS:  # per-IP keys: drop idle ones
                cutoff = now - self.window_s
                for idle in [k for k, h in self._hits.items() if not h or h[-1] <= cutoff]:
                    del self._hits[idle]
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= now - self.window_s:
                hits.popleft()
            if len(hits) >= self.limit:
                oldest = hits[0] if hits else now
                return max(1, int(oldest + self.window_s - now) + 1)
            hits.append(now)
            return None


class VisitorRuns:
    """Which agent runs each anonymous demo visitor created, in this process only.

    Every anonymous visitor acts as the one shared demo user, and agent_runs has no column for
    the visitor, so run listings and run detail for visitors are limited to the run ids recorded
    here: a visitor never sees another visitor's runs, and after a restart sees none (an empty
    list is a valid AgentRunList). Bounded: least recently active visitors are dropped first."""

    def __init__(
        self, max_visitors: int = MAX_VISITORS, max_runs: int = MAX_RUNS_PER_VISITOR
    ) -> None:
        self.max_visitors = max_visitors
        self.max_runs = max_runs
        self._runs: OrderedDict[str, deque[uuid.UUID]] = OrderedDict()
        self._lock = threading.Lock()

    def add(self, visitor: str, run_id: uuid.UUID) -> None:
        with self._lock:
            runs = self._runs.pop(visitor, None) or deque(maxlen=self.max_runs)
            if run_id not in runs:
                runs.append(run_id)
            self._runs[visitor] = runs
            while len(self._runs) > self.max_visitors:
                self._runs.popitem(last=False)

    def of(self, visitor: str) -> list[uuid.UUID]:
        with self._lock:
            return list(self._runs.get(visitor, ()))


@dataclass
class Services:
    settings: Settings
    verifier: TokenVerifier
    db: Database | None
    store_factory: Callable[[], EvidenceStore]
    ask_limiter: RateLimiter
    # Bounds concurrent /ask runs (each holds two pool connections for the whole run).
    ask_slots: threading.BoundedSemaphore
    # The agent entry point (`caselens_agent.runner.ask`); tests swap in a fake router.
    ask_kwargs: dict[str, Any] = field(default_factory=dict)
    visitor_runs: VisitorRuns = field(default_factory=VisitorRuns)
    _store: EvidenceStore | None = None

    def require_db(self) -> Database:
        if self.db is None:
            raise ProblemError(503, "The database is not configured (CASELENS_API_DATABASE_URL).")
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
    """One request's DB connection (caselens_app, app.user_id = user.id) and its user.
    `visitor` is the Firebase uid of an anonymous demo visitor (who acts as the shared demo
    user), None for everyone else."""

    conn: Conn
    user: UserRow
    services: Services
    visitor: str | None


@contextmanager
def authenticated_session(services: Services, authorization: str | None) -> Iterator[Session]:
    """Verify the bearer token, then hold one pooled connection as the resolved user.
    Anonymous Firebase tokens are the shared demo viewer when CASELENS_ANONYMOUS_DEMO is on and
    401 otherwise (no users row per anonymous uid)."""
    identity = verify_or_401(services.verifier, authorization)
    if identity.is_anonymous and not services.settings.anonymous_demo:
        raise unauthorized("Anonymous sign-in is not enabled.")
    db = services.require_db()
    with db.session(None) as conn:
        if identity.is_anonymous:
            user, visitor = resolve_demo_viewer(conn), identity.subject
        else:
            user, visitor = resolve_user(conn, identity), None
        set_user(conn, user.id)
        yield Session(conn, user, services, visitor)


def get_session(request: Request, services: ServicesDep) -> Iterator[Session]:
    with authenticated_session(services, request.headers.get("authorization")) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


@dataclass
class CaseSession(Session):
    case_id: uuid.UUID
    role: str


def _case_session(minimum: str) -> Callable[[str, Session], CaseSession]:
    def dependency(case_id: str, session: SessionDep) -> CaseSession:
        cid = parse_id(case_id, "case")
        role = require_role(session.conn, cid, minimum)
        return CaseSession(session.conn, session.user, session.services, session.visitor, cid, role)

    return dependency


ViewerCase = Annotated[CaseSession, Depends(_case_session("viewer"))]
AnalystCase = Annotated[CaseSession, Depends(_case_session("analyst"))]
LeadCase = Annotated[CaseSession, Depends(_case_session("lead"))]


def get_admin(session: SessionDep) -> Session:
    if not session.user.is_admin:
        raise ProblemError(403, "This endpoint needs a platform admin.")
    return session


AdminSession = Annotated[Session, Depends(get_admin)]
