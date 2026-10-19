"""Ingest job state machine on `ingest_jobs`, keyed on (evidence_id, stage).

queued -> running (claim, attempts += 1) -> succeeded | failed -> queued again after backoff
while attempts < max_attempts, else dead_lettered. Each transition writes a system audit event
(user_id NULL, migration 0003) into the hash-chained audit_log.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from typing import Protocol

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict

from caselens_workers.db import Conn


class JobMessage(BaseModel):
    """Queue message (Cloud Tasks body in prod, an ingest_jobs row locally)."""

    model_config = ConfigDict(frozen=True)

    job_id: uuid.UUID
    case_id: uuid.UUID
    evidence_id: uuid.UUID
    stage: str
    attempt: int


class Queue(Protocol):
    def enqueue(self, conn: Conn, case_id: uuid.UUID, evidence_id: uuid.UUID, stage: str) -> None:
        """Make sure a (evidence_id, stage) job exists. A duplicate enqueue is a no-op."""


class PostgresQueue:
    """Local queue: the ingest_jobs table itself, claimed with FOR UPDATE SKIP LOCKED."""

    def enqueue(self, conn: Conn, case_id: uuid.UUID, evidence_id: uuid.UUID, stage: str) -> None:
        conn.execute(
            "INSERT INTO public.ingest_jobs (case_id, evidence_id, stage) VALUES (%s, %s, %s) "
            "ON CONFLICT (evidence_id, stage) DO NOTHING",
            (case_id, evidence_id, stage),
        )


def audit(conn: Conn, case_id: uuid.UUID, action: str, payload: dict[str, object]) -> None:
    """System audit event: no user_id (the column is not even granted to the worker role)."""
    conn.execute(
        "INSERT INTO public.audit_log (case_id, action, payload) VALUES (%s, %s, %s)",
        (case_id, action, Jsonb(payload)),
    )


def claim_next(
    conn: Conn,
    stages: Collection[str],
    max_attempts: int,
    stale_after_s: int,
) -> JobMessage | None:
    """Claim one runnable job: queued, failed past its backoff, or running but stale (crashed)."""
    row = conn.execute(
        """
        UPDATE public.ingest_jobs AS j
           SET status = 'running', attempts = j.attempts + 1, started_at = now(),
               finished_at = NULL, updated_at = now()
         WHERE j.id = (
               SELECT id FROM public.ingest_jobs
                WHERE stage = ANY(%(stages)s)
                  AND attempts < %(max_attempts)s
                  AND (status = 'queued'
                       OR (status = 'failed'
                           AND updated_at < now() - make_interval(secs => power(2, attempts)))
                       OR (status = 'running'
                           AND started_at < now() - make_interval(secs => %(stale)s)))
                ORDER BY created_at, id
                LIMIT 1
                FOR UPDATE SKIP LOCKED)
        RETURNING j.id, j.case_id, j.evidence_id, j.stage, j.attempts
        """,
        {"stages": list(stages), "max_attempts": max_attempts, "stale": stale_after_s},
    ).fetchone()
    if row is None:
        return None
    return JobMessage(
        job_id=row[0], case_id=row[1], evidence_id=row[2], stage=row[3], attempt=row[4]
    )


def mark_succeeded(conn: Conn, job: JobMessage, summary: dict[str, object]) -> None:
    conn.execute(
        "UPDATE public.ingest_jobs SET status = 'succeeded', error = NULL, finished_at = now(), "
        "updated_at = now() WHERE id = %s",
        (job.job_id,),
    )
    audit(
        conn,
        job.case_id,
        "ingest.stage_succeeded",
        {
            "evidence_id": str(job.evidence_id),
            "stage": job.stage,
            "attempt": job.attempt,
            **summary,
        },
    )


def mark_failed(conn: Conn, job: JobMessage, error: str, max_attempts: int) -> str:
    """Record a failure. Returns the new status ('failed' to retry, or 'dead_lettered')."""
    status = "dead_lettered" if job.attempt >= max_attempts else "failed"
    conn.execute(
        "UPDATE public.ingest_jobs SET status = %s, error = %s, finished_at = now(), "
        "updated_at = now() WHERE id = %s",
        (status, error[:4000], job.job_id),
    )
    action = "ingest.dead_lettered" if status == "dead_lettered" else "ingest.stage_failed"
    audit(
        conn,
        job.case_id,
        action,
        {
            "evidence_id": str(job.evidence_id),
            "stage": job.stage,
            "attempt": job.attempt,
            "error": error[:500],
        },
    )
    return status


def requeue(conn: Conn, evidence_id: uuid.UUID, stage: str) -> bool:
    """Operator action: put one stage back to queued with a fresh attempt budget."""
    cur = conn.execute(
        "UPDATE public.ingest_jobs SET status = 'queued', attempts = 0, error = NULL, "
        "started_at = NULL, finished_at = NULL, updated_at = now() "
        "WHERE evidence_id = %s AND stage = %s",
        (evidence_id, stage),
    )
    return cur.rowcount == 1
