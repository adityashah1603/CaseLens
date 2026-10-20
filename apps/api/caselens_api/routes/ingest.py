"""Live ingest progress over Server-Sent Events.

There is no LISTEN/NOTIFY trigger on ingest_jobs (adding one needs a migration), so the stream
polls the case's jobs every `CASELENS_SSE_POLL_S` seconds and diffs them against what it last saw.
Diffing a snapshot (instead of filtering on `updated_at > watermark`) cannot miss an update from a
long worker transaction whose `now()` is older than the watermark.

* `job.updated` for every job whose (status, attempts, error, updated_at) changed.
* `evidence.ready` when every job of an evidence file has succeeded (the workers enqueue the next
  stage in the same transaction that finishes a stage, so "all succeeded" means done).
* `evidence.failed` when a job of the file is dead-lettered.
* `: heartbeat` comments every `CASELENS_SSE_HEARTBEAT_S` seconds.

Event ids are monotonic per stream. On reconnect with `Last-Event-ID: N`, numbering resumes at
N + 1 and the stream first re-sends the current state of every job (`job.updated`), so the client
never misses a change made while it was disconnected. A fresh stream (no Last-Event-ID) starts
from the current state silently; load the evidence list first, then open the stream.
Each poll checks out a pooled connection briefly (as caselens_app with app.user_id), and the
stream ends if the caller stops being a member of the case.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from caselens_api import models as m
from caselens_api.common import case_role, parse_id, require_role
from caselens_api.deps import Services, Session, get_session
from caselens_api.routes.evidence import job_from_row

router = APIRouter(prefix="/v1/cases/{case_id}")

_CASE_JOBS = """
SELECT j.id, j.case_id, j.evidence_id, j.stage, j.status, j.attempts, j.error, j.started_at,
       j.finished_at, j.created_at, j.updated_at
  FROM public.ingest_jobs AS j
 WHERE j.case_id = %s
 ORDER BY j.created_at, j.id
"""


@dataclass
class _Snapshot:
    member: bool
    jobs: list[m.IngestJob]


def _poll(services: Services, user_id: uuid.UUID, case_id: uuid.UUID) -> _Snapshot:
    with services.require_db().session(user_id) as conn:
        if case_role(conn, case_id) is None:
            return _Snapshot(False, [])
        rows = conn.execute(_CASE_JOBS, (case_id,)).fetchall()
    return _Snapshot(True, [job_from_row(r) for r in rows])


def _key(job: m.IngestJob) -> tuple[Any, ...]:
    return (job.status, job.attempts, job.error, job.updated_at)


def _evidence_state(jobs: list[m.IngestJob]) -> dict[uuid.UUID, str]:
    """Per evidence file: 'ready' (all succeeded), 'failed' (any dead-lettered) or 'busy'."""
    by_ev: dict[uuid.UUID, list[str]] = {}
    for job in jobs:
        by_ev.setdefault(job.evidence_id, []).append(job.status)
    out = {}
    for ev, statuses in by_ev.items():
        if "dead_lettered" in statuses:
            out[ev] = "failed"
        elif all(s == "succeeded" for s in statuses):
            out[ev] = "ready"
        else:
            out[ev] = "busy"
    return out


class _Stream:
    def __init__(self, case_id: uuid.UUID, last_event_id: int) -> None:
        self.case_id = case_id
        self.next_id = last_event_id + 1

    def frame(self, event: str, evidence_id: uuid.UUID, job: m.IngestJob | None) -> str:
        payload = m.IngestEvent(
            event=event,  # type: ignore[arg-type]
            case_id=self.case_id,
            evidence_id=evidence_id,
            job=job,
            emitted_at=datetime.now(UTC),
        )
        data = json.dumps(payload.model_dump(mode="json"), separators=(",", ":"))
        frame = f"id: {self.next_id}\nevent: {event}\ndata: {data}\n\n"
        self.next_id += 1
        return frame


def _parse_last_event_id(raw: str | None) -> int | None:
    if raw is None or not raw.strip().isdigit():
        return None
    return int(raw.strip())


def _check_viewer(
    case_id: str, session: Annotated[Session, Depends(get_session, scope="function")]
) -> Session:
    require_role(session.conn, parse_id(case_id, "case"), "viewer")
    return session


@router.get("/ingest/events")
async def stream_ingest_events(
    case_id: str,
    request: Request,
    # scope="function": the request's pooled connection goes back before the stream starts.
    session: Annotated[Session, Depends(_check_viewer, scope="function")],
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    cid = parse_id(case_id, "case")
    services, user_id = session.services, session.user.id
    settings = services.settings
    resume = _parse_last_event_id(last_event_id)

    async def events() -> AsyncIterator[str]:
        stream = _Stream(cid, resume or 0)
        snap = await run_in_threadpool(_poll, services, user_id, cid)
        seen = {j.id: _key(j) for j in snap.jobs}
        states = _evidence_state(snap.jobs)
        yield "retry: 3000\n\n"
        if resume is not None:
            for job in snap.jobs:
                yield stream.frame("job.updated", job.evidence_id, job)
        started = last_beat = time.monotonic()
        while snap.member and time.monotonic() - started < settings.sse_max_stream_s:
            await asyncio.sleep(settings.sse_poll_s)
            if await request.is_disconnected():
                return
            snap = await run_in_threadpool(_poll, services, user_id, cid)
            if not snap.member:
                return
            for job in snap.jobs:
                key = _key(job)
                if seen.get(job.id) != key:
                    seen[job.id] = key
                    yield stream.frame("job.updated", job.evidence_id, job)
            new_states = _evidence_state(snap.jobs)
            for ev, state in new_states.items():
                if state != states.get(ev) and state in ("ready", "failed"):
                    name = "evidence.ready" if state == "ready" else "evidence.failed"
                    yield stream.frame(name, ev, None)
            states = new_states
            if time.monotonic() - last_beat >= settings.sse_heartbeat_s:
                last_beat = time.monotonic()
                yield ": heartbeat\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
