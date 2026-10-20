"""CSV pipeline: schema_infer -> validate -> load -> register -> embed, on SYNTHETIC files."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from caselens_workers import jobs
from caselens_workers.db import Conn
from caselens_workers.pipeline import Pipeline, run_job
from caselens_workers.pipelines import csv as csv_pipe
from caselens_workers.settings import WorkerSettings
from caselens_workers.stages.embed import EMBEDDING_DIM, make_embed_stage
from caselens_workers.testing import SeededEvidence, as_role

pytestmark = pytest.mark.db

NOTICE = "# SYNTHETIC DATA - fictional test file. No real people, numbers or events.\n"

CALLS = NOTICE + (
    "row_id,caller_number,callee_number,started_at,duration_s,call_type,cell_tower_id\n"
    "1,+13605550142,(360) 555-0117,2026-03-14T22:31:05-07:00,94,voice,TWR-PC-117\n"
    "2,360.555.0117,+13605550142,2026-03-14T22:35:00-07:00,0,SMS,\n"
)
PLATES = NOTICE + (
    "row_id,plate,plate_state,read_at,camera_id,location_name,latitude,longitude,confidence\n"
    "1,syn-4821,WA,2026-03-14T22:41:05-07:00,alpr_test,Test Rd at Fake Ave,36.9,-123.4,0.91\n"
    "2,SYN4821,WA,2026-03-14T22:47:30-07:00,alpr_test,Test Rd at Fake Ave,36.9,-123.4,0.88\n"
)
CUSTODY = NOTICE + (
    "row_id,item_id,description,collected_by,collected_at,collected_location,custody_chain\n"
    '1,ITEM-001,"One brass casing, fired",Tech A (SYNTHETIC),2026-03-14T23:24:00-07:00,'
    '"Test lot, placard 1",2026-03-14T23:24:00-07:00 Tech A - Collected\n'
)


def _fake_embedder(texts: Sequence[str]) -> list[list[float]]:
    return [[0.25] * EMBEDDING_DIM for _ in texts]


PIPE: Pipeline = dataclasses.replace(
    csv_pipe.PIPELINE,
    stages={**csv_pipe.PIPELINE.stages, "embed": make_embed_stage(_fake_embedder)},
)


def _seed(
    seed_evidence: Callable[..., SeededEvidence], tmp_path: Path, name: str, body: str
) -> SeededEvidence:
    f = tmp_path / name
    f.write_text(body, encoding="utf-8")
    return seed_evidence(f, "csv")


def _start(conn: Conn, ev: SeededEvidence) -> None:
    jobs.PostgresQueue().enqueue(conn, ev.case_id, ev.evidence_id, PIPE.first)
    as_role(conn, "caselens_worker")


def _drain(conn: Conn, ev: SeededEvidence, settings: WorkerSettings) -> list[str]:
    out = []
    while (job := jobs.claim_next(conn, set(PIPE.stages), 3, 1800)) is not None:
        status = run_job(conn, job, {"csv": PIPE}, ev.store, settings, jobs.PostgresQueue())
        out.append(f"{job.stage}:{status}")
    return out


def test_call_records_load_register_and_rerun_idempotent(
    owner_conn: Conn,
    seed_evidence: Callable[..., SeededEvidence],
    tmp_path: Path,
    worker_settings: WorkerSettings,
) -> None:
    ev = _seed(seed_evidence, tmp_path, "calls.csv", CALLS)
    _start(owner_conn, ev)
    assert _drain(owner_conn, ev, worker_settings) == [
        "schema_infer:succeeded",
        "validate:succeeded",
        "load:succeeded",
        "register:succeeded",
        "embed:succeeded",
    ]
    rows = owner_conn.execute(
        "SELECT row_id, caller_number, callee_number, call_type, cell_tower_id, case_id "
        "FROM public.call_records WHERE evidence_id = %s ORDER BY row_id",
        (ev.evidence_id,),
    ).fetchall()
    assert rows == [
        (1, "+13605550142", "+13605550117", "voice", "TWR-PC-117", ev.case_id),
        (2, "+13605550117", "+13605550142", "sms", None, ev.case_id),
    ]
    chunks = owner_conn.execute(
        "SELECT chunk_index, text, locator, t_start IS NOT NULL, vector_dims(embedding) "
        "FROM public.chunks WHERE evidence_id = %s ORDER BY chunk_index",
        (ev.evidence_id,),
    ).fetchall()
    assert chunks[0][1] == (
        "Call record row 1: +13605550142 called +13605550117 at 2026-03-14 22:31:05-07:00, "
        "voice, 94 s, tower TWR-PC-117"
    )
    assert chunks[1][2] == {"kind": "record", "table": "call_records", "row_id": 2}
    assert all(c[3] and c[4] == EMBEDDING_DIM for c in chunks)
    entities = owner_conn.execute(
        "SELECT type, normalized_key FROM public.entities WHERE case_id = %s ORDER BY 2",
        (ev.case_id,),
    ).fetchall()
    assert entities == [("phone", "+13605550117"), ("phone", "+13605550142")]

    def counts() -> tuple[object, ...]:
        row = owner_conn.execute(
            "SELECT (SELECT count(*) FROM public.call_records WHERE evidence_id = %(e)s), "
            "(SELECT count(*) FROM public.chunks WHERE evidence_id = %(e)s), "
            "(SELECT count(*) FROM public.mentions WHERE case_id = %(c)s), "
            "(SELECT count(*) FROM public.entities WHERE case_id = %(c)s)",
            {"e": ev.evidence_id, "c": ev.case_id},
        ).fetchone()
        assert row is not None
        return tuple(row)

    assert counts() == (2, 2, 4, 2)
    for stage in ("load", "register"):  # one at a time: claim order within a txn is by id
        assert jobs.requeue(owner_conn, ev.evidence_id, stage)
        assert _drain(owner_conn, ev, worker_settings) == [f"{stage}:succeeded"]
    assert counts() == (2, 2, 4, 2)


def test_plate_reads_normalize_plates_and_share_vehicle_entity(
    owner_conn: Conn,
    seed_evidence: Callable[..., SeededEvidence],
    tmp_path: Path,
    worker_settings: WorkerSettings,
) -> None:
    ev = _seed(seed_evidence, tmp_path, "plates.csv", PLATES)
    _start(owner_conn, ev)
    assert _drain(owner_conn, ev, worker_settings)[-1] == "embed:succeeded"
    plates = owner_conn.execute(
        "SELECT plate, read_at, confidence FROM public.plate_reads WHERE evidence_id = %s "
        "ORDER BY row_id",
        (ev.evidence_id,),
    ).fetchall()
    assert [p[0] for p in plates] == ["SYN4821", "SYN4821"]
    assert plates[0][1].utcoffset() is not None
    vehicles = owner_conn.execute(
        "SELECT e.normalized_key, count(m.id), min(m.confidence) FROM public.entities e "
        "JOIN public.mentions m ON m.entity_id = e.id WHERE e.case_id = %s "
        "GROUP BY e.normalized_key",
        (ev.case_id,),
    ).fetchall()
    assert vehicles == [("SYN4821", 2, 1.0)]
    text = owner_conn.execute(
        "SELECT text FROM public.chunks WHERE evidence_id = %s AND chunk_index = 0",
        (ev.evidence_id,),
    ).fetchone()
    assert text is not None and "plate SYN4821 (WA) read at 2026-03-14 22:41:05-07:00" in text[0]


def test_custody_records_have_no_identifier_entities(
    owner_conn: Conn,
    seed_evidence: Callable[..., SeededEvidence],
    tmp_path: Path,
    worker_settings: WorkerSettings,
) -> None:
    ev = _seed(seed_evidence, tmp_path, "evidence_log.csv", CUSTODY)
    _start(owner_conn, ev)
    assert _drain(owner_conn, ev, worker_settings)[-1] == "embed:succeeded"
    row = owner_conn.execute(
        "SELECT item_id, description, collected_location FROM public.custody_records "
        "WHERE evidence_id = %s",
        (ev.evidence_id,),
    ).fetchone()
    assert row == ("ITEM-001", "One brass casing, fired", "Test lot, placard 1")
    n = owner_conn.execute(
        "SELECT count(*) FROM public.entities WHERE case_id = %s", (ev.case_id,)
    ).fetchone()
    assert n == (0,)


def _first_error(conn: Conn, ev: SeededEvidence, settings: WorkerSettings) -> tuple[str, str]:
    for _ in range(3):
        job = jobs.claim_next(conn, set(PIPE.stages), 3, 1800)
        assert job is not None
        status = run_job(conn, job, {"csv": PIPE}, ev.store, settings, jobs.PostgresQueue())
        if status != "succeeded":
            row = conn.execute(
                "SELECT error FROM public.ingest_jobs WHERE id = %s", (job.job_id,)
            ).fetchone()
            assert row is not None
            return job.stage, str(row[0])
    raise AssertionError("no stage failed")


def test_unknown_header_fails_schema_infer(
    owner_conn: Conn,
    seed_evidence: Callable[..., SeededEvidence],
    tmp_path: Path,
    worker_settings: WorkerSettings,
) -> None:
    ev = _seed(seed_evidence, tmp_path, "odd.csv", NOTICE + "row_id,color,shape\n1,gray,round\n")
    _start(owner_conn, ev)
    stage, error = _first_error(owner_conn, ev, worker_settings)
    assert stage == "schema_infer"
    assert "CsvSchemaError" in error and "does not match exactly one table" in error


def test_invalid_rows_fail_validate_naming_row_ids(
    owner_conn: Conn,
    seed_evidence: Callable[..., SeededEvidence],
    tmp_path: Path,
    worker_settings: WorkerSettings,
) -> None:
    body = CALLS + (
        "3,+13605550142,+13605550117,2026-03-14T22:40:00,5,voice,T1\n"  # naive timestamp
        "4,555-01,+13605550117,2026-03-14T22:41:00-07:00,5,voice,T1\n"  # bad phone
    )
    ev = _seed(seed_evidence, tmp_path, "calls_bad.csv", body)
    _start(owner_conn, ev)
    stage, error = _first_error(owner_conn, ev, worker_settings)
    assert stage == "validate"
    assert "2 invalid row(s) in call_records" in error
    assert "row_id 3: started_at" in error and "row_id 4: caller_number" in error
    n = owner_conn.execute(
        "SELECT count(*) FROM public.call_records WHERE evidence_id = %s", (ev.evidence_id,)
    ).fetchone()
    assert n == (0,)


def test_infer_table_rules() -> None:
    header = list(csv_pipe.TABLE_COLUMNS["custody_records"])
    assert csv_pipe.infer_table(header) == "custody_records"
    assert csv_pipe.infer_table(["row_id", "plate", "read_at"]) == "plate_reads"
    with pytest.raises(csv_pipe.CsvSchemaError, match="first column must be row_id"):
        csv_pipe.infer_table(["plate", "row_id", "read_at"])
    with pytest.raises(csv_pipe.CsvSchemaError):
        csv_pipe.infer_table(["row_id", "plate"])  # read_at is required
