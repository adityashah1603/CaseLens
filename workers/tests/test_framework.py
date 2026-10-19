from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path

import pytest

from caselens_workers import jobs
from caselens_workers.chunks import ChunkIn, PdfLocator, RecordLocator, replace_chunks
from caselens_workers.context import StageContext, StageResult
from caselens_workers.db import Conn
from caselens_workers.pipeline import Pipeline, run_job
from caselens_workers.settings import WorkerSettings, libpq_url
from caselens_workers.stages.embed import EMBEDDING_DIM, make_embed_stage
from caselens_workers.storage import MemoryObjectStore, artifact_key
from caselens_workers.testing import SeededEvidence, as_role

pytestmark = pytest.mark.db


def _fake_embedder(texts: object) -> list[list[float]]:
    return [[0.5] * EMBEDDING_DIM for _ in texts]  # type: ignore[attr-defined]


def _chunk_stage(ctx: StageContext) -> StageResult:
    path = ctx.original_path()
    ctx.put_json("pages.json", {"text": path.read_text(encoding="utf-8")})
    replace_chunks(
        ctx,
        [
            ChunkIn(chunk_index=0, text="SYNTHETIC gray sedan", locator=PdfLocator(page=1)),
            ChunkIn(
                chunk_index=1,
                text="SYNTHETIC row",
                locator=RecordLocator(table="call_records", row_id=7),
            ),
        ],
    )
    return StageResult(summary={"chunks": 2})


def _boom(ctx: StageContext) -> StageResult:
    raise RuntimeError("SYNTHETIC failure")


PIPE = Pipeline(
    first="chunk",
    stages={"chunk": _chunk_stage, "embed": make_embed_stage(_fake_embedder), "boom": _boom},
    next={"chunk": "embed", "embed": None, "boom": None},
)


@pytest.fixture
def evidence(seed_evidence: Callable[..., SeededEvidence], tmp_path: Path) -> SeededEvidence:
    f = tmp_path / "doc.pdf"
    f.write_text("SYNTHETIC content", encoding="utf-8")
    return seed_evidence(f, "pdf")


def _claim(conn: Conn, stages: set[str]) -> jobs.JobMessage | None:
    return jobs.claim_next(conn, stages, max_attempts=3, stale_after_s=1800)


def _drain(conn: Conn, ev: SeededEvidence, settings: WorkerSettings) -> list[str]:
    statuses = []
    while (job := _claim(conn, set(PIPE.stages))) is not None:
        status = run_job(conn, job, {"pdf": PIPE}, ev.store, settings, jobs.PostgresQueue())
        statuses.append(f"{job.stage}:{status}")
    return statuses


def test_pipeline_runs_stages_in_order_and_audits(
    owner_conn: Conn, evidence: SeededEvidence, worker_settings: WorkerSettings
) -> None:
    jobs.PostgresQueue().enqueue(owner_conn, evidence.case_id, evidence.evidence_id, "chunk")
    as_role(owner_conn, "caselens_worker")
    assert _drain(owner_conn, evidence, worker_settings) == ["chunk:succeeded", "embed:succeeded"]

    rows = owner_conn.execute(
        "SELECT chunk_index, locator->>'kind', vector_dims(embedding) FROM public.chunks "
        "WHERE evidence_id = %s ORDER BY chunk_index",
        (evidence.evidence_id,),
    ).fetchall()
    assert rows == [(0, "pdf", EMBEDDING_DIM), (1, "record", EMBEDDING_DIM)]
    key = artifact_key(evidence.case_id, evidence.evidence_id, "chunk", "pages.json")
    assert key in evidence.store.objects

    as_role(owner_conn, None)
    audit = owner_conn.execute(
        "SELECT action, user_id, payload->>'stage' FROM public.audit_log WHERE case_id = %s "
        "ORDER BY id",
        (evidence.case_id,),
    ).fetchall()
    assert audit == [
        ("ingest.stage_succeeded", None, "chunk"),
        ("ingest.stage_succeeded", None, "embed"),
    ]


def test_rerun_is_idempotent(
    owner_conn: Conn, evidence: SeededEvidence, worker_settings: WorkerSettings
) -> None:
    queue = jobs.PostgresQueue()
    queue.enqueue(owner_conn, evidence.case_id, evidence.evidence_id, "chunk")
    queue.enqueue(owner_conn, evidence.case_id, evidence.evidence_id, "chunk")  # duplicate: no-op
    as_role(owner_conn, "caselens_worker")
    _drain(owner_conn, evidence, worker_settings)
    assert jobs.requeue(owner_conn, evidence.evidence_id, "chunk")
    assert _drain(owner_conn, evidence, worker_settings) == ["chunk:succeeded"]  # embed done
    count = owner_conn.execute(
        "SELECT count(*) FROM public.chunks WHERE evidence_id = %s", (evidence.evidence_id,)
    ).fetchone()
    assert count == (2,)


def test_failures_retry_then_dead_letter(
    owner_conn: Conn, evidence: SeededEvidence, worker_settings: WorkerSettings
) -> None:
    jobs.PostgresQueue().enqueue(owner_conn, evidence.case_id, evidence.evidence_id, "boom")
    as_role(owner_conn, "caselens_worker")
    queue = jobs.PostgresQueue()
    seen = []
    for _ in range(3):
        job = _claim(owner_conn, {"boom"})
        assert job is not None
        seen.append(run_job(owner_conn, job, {"pdf": PIPE}, evidence.store, worker_settings, queue))
        # Skip the backoff wait.
        owner_conn.execute(
            "UPDATE public.ingest_jobs SET updated_at = now() - interval '1 hour' WHERE id = %s",
            (job.job_id,),
        )
    assert seen == ["failed", "failed", "dead_lettered"]
    assert _claim(owner_conn, {"boom"}) is None
    row = owner_conn.execute(
        "SELECT status, attempts, error FROM public.ingest_jobs WHERE evidence_id = %s",
        (evidence.evidence_id,),
    ).fetchone()
    assert row is not None and row[:2] == ("dead_lettered", 3) and "SYNTHETIC failure" in row[2]


def test_integrity_mismatch_fails_and_audits(
    owner_conn: Conn, evidence: SeededEvidence, worker_settings: WorkerSettings
) -> None:
    for key in evidence.store.objects:
        evidence.store.objects[key] = b"tampered"
    jobs.PostgresQueue().enqueue(owner_conn, evidence.case_id, evidence.evidence_id, "chunk")
    as_role(owner_conn, "caselens_worker")
    job = _claim(owner_conn, {"chunk"})
    assert job is not None
    status = run_job(
        owner_conn, job, {"pdf": PIPE}, evidence.store, worker_settings, jobs.PostgresQueue()
    )
    assert status == "failed"
    as_role(owner_conn, None)
    actions = [
        r[0]
        for r in owner_conn.execute(
            "SELECT action FROM public.audit_log WHERE case_id = %s ORDER BY id",
            (evidence.case_id,),
        ).fetchall()
    ]
    assert actions == ["evidence.integrity_check", "ingest.stage_failed"]


def test_worker_cannot_write_audit_as_a_user(owner_conn: Conn, evidence: SeededEvidence) -> None:
    import psycopg

    as_role(owner_conn, "caselens_worker")
    with pytest.raises(psycopg.errors.InsufficientPrivilege), owner_conn.transaction():
        owner_conn.execute(
            "INSERT INTO public.audit_log (user_id, case_id, action) VALUES (%s, %s, 'x')",
            (evidence.user_id, evidence.case_id),
        )


def test_submit_seeds_through_rls(migrated_url: str, owner_conn: Conn, tmp_path: Path) -> None:
    import hashlib
    import json

    from caselens_workers.submit import submit_case

    case_dir = tmp_path / "case"
    (case_dir / "csv").mkdir(parents=True)
    f = case_dir / "csv" / "calls.csv"
    f.write_text("# SYNTHETIC\nrow_id\n1\n", encoding="utf-8")
    (case_dir / "manifest.json").write_text(
        json.dumps(
            {
                "notice": "SYNTHETIC",
                "files": [
                    {
                        "path": "csv/calls.csv",
                        "type": "csv",
                        "sha256": hashlib.sha256(f.read_bytes()).hexdigest(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    store = MemoryObjectStore()
    name = f"SYNTHETIC seed {uuid.uuid4()}"
    as_role(owner_conn, "caselens_app")
    first = submit_case(owner_conn, store, case_dir, name, {"csv": "schema_infer"})
    again = submit_case(owner_conn, store, case_dir, name, {"csv": "schema_infer"})
    assert first.uploaded == ["csv/calls.csv"] and again.skipped == ["csv/calls.csv"]
    assert first.case_id == again.case_id
    as_role(owner_conn, None)
    jobs_rows = owner_conn.execute(
        "SELECT stage, status FROM public.ingest_jobs WHERE case_id = %s", (first.case_id,)
    ).fetchall()
    assert jobs_rows == [("schema_infer", "queued")]
    actions = owner_conn.execute(
        "SELECT action, user_id IS NOT NULL FROM public.audit_log WHERE case_id = %s ORDER BY id",
        (first.case_id,),
    ).fetchall()
    assert actions == [("case.create", True), ("evidence.upload", True)]
    assert libpq_url("postgresql+psycopg://u@h/db") == "postgresql://u@h/db"
