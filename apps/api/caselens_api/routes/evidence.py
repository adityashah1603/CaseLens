"""Evidence: list, upload (multipart), metadata, audited content redirect, ingest jobs."""

from __future__ import annotations

import hashlib
import os
import uuid
from datetime import UTC, datetime
from functools import lru_cache
from typing import Annotated, Any, BinaryIO

import psycopg
from fastapi import APIRouter, Query, Request
from fastapi.responses import RedirectResponse
from psycopg.types.json import Jsonb
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from caselens_api import models as m
from caselens_api.common import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    cursor_params,
    decode_cursor,
    encode_cursor,
    parse_id,
    require_role,
    write_audit,
)
from caselens_api.db import Conn
from caselens_api.deps import StoreDep, ViewerCase
from caselens_api.problems import ProblemError, conflict, not_found, validation
from caselens_api.storage import EvidenceStore, StorageError
from caselens_workers.storage import evidence_key

router = APIRouter(prefix="/v1/cases/{case_id}")

Cursor = Annotated[str | None, Query(max_length=512)]
Limit = Annotated[int, Query(ge=1, le=MAX_LIMIT)]
HASH_CHUNK = 1024 * 1024
MAX_DESCRIPTION = 2000
DUPLICATE = "A file with this SHA-256 already exists in this case."


# ---- reading -------------------------------------------------------------------------------

_EVIDENCE_SELECT = """
SELECT e.id, e.case_id, e.type, e.original_filename, e.content_type, e.size_bytes, e.sha256,
       e.uploaded_by, e.chain_of_custody, e.recorded_at, e.created_at
  FROM public.evidence AS e
 WHERE e.case_id = %(case_id)s
"""
_EVIDENCE_ONE = _EVIDENCE_SELECT + " AND e.id = %(evidence_id)s"
_EVIDENCE_PAGE = (
    _EVIDENCE_SELECT
    + """
   AND (%(type)s::text IS NULL OR e.type = %(type)s::text)
   AND (%(c_ts)s::timestamptz IS NULL OR (e.created_at, e.id) > (%(c_ts)s, %(c_id)s::uuid))
 ORDER BY e.created_at, e.id
 LIMIT %(limit)s
"""
)
_JOBS_FOR = """
SELECT j.id, j.case_id, j.evidence_id, j.stage, j.status, j.attempts, j.error, j.started_at,
       j.finished_at, j.created_at, j.updated_at
  FROM public.ingest_jobs AS j
 WHERE j.case_id = %(case_id)s AND j.evidence_id = ANY(%(ids)s::uuid[])
 ORDER BY j.created_at, j.id
"""


def job_from_row(row: tuple[Any, ...]) -> m.IngestJob:
    return m.IngestJob(
        id=row[0],
        case_id=row[1],
        evidence_id=row[2],
        stage=row[3],
        status=row[4],
        attempts=row[5],
        error=row[6],
        started_at=row[7],
        finished_at=row[8],
        created_at=row[9],
        updated_at=row[10],
    )


def _jobs(
    conn: Conn, case_id: uuid.UUID, ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[m.IngestJob]]:
    out: dict[uuid.UUID, list[m.IngestJob]] = {i: [] for i in ids}
    if ids:
        for row in conn.execute(_JOBS_FOR, {"case_id": case_id, "ids": ids}).fetchall():
            out[row[2]].append(job_from_row(row))
    return out


def _evidence(row: tuple[Any, ...], jobs: list[m.IngestJob]) -> m.Evidence:
    custody = row[8] if isinstance(row[8], list) else []
    return m.Evidence(
        id=row[0],
        case_id=row[1],
        type=row[2],
        original_filename=row[3],
        content_type=row[4],
        size_bytes=row[5],
        sha256=row[6].strip(),
        uploaded_by=row[7],
        chain_of_custody=[m.CustodyEntry.model_validate(c) for c in custody],
        recorded_at=row[9],
        ingest_jobs=jobs,
        created_at=row[10],
    )


def load_evidence(conn: Conn, case_id: uuid.UUID, evidence_id: uuid.UUID) -> m.Evidence:
    row = conn.execute(_EVIDENCE_ONE, {"case_id": case_id, "evidence_id": evidence_id}).fetchone()
    if row is None:
        raise not_found("No such evidence.")
    return _evidence(row, _jobs(conn, case_id, [evidence_id])[evidence_id])


@router.get("/evidence", response_model=m.EvidenceList)
def list_evidence(
    case: ViewerCase,
    type: m.EvidenceType | None = None,
    cursor: Cursor = None,
    limit: Limit = DEFAULT_LIMIT,
) -> m.EvidenceList:
    after = decode_cursor(cursor, (datetime, uuid.UUID))
    rows = case.conn.execute(
        _EVIDENCE_PAGE,
        {
            "case_id": case.case_id,
            "type": type,
            **cursor_params(after, ("c_ts", "c_id")),
            "limit": limit + 1,
        },
    ).fetchall()
    page = rows[:limit]
    jobs = _jobs(case.conn, case.case_id, [r[0] for r in page])
    nxt = encode_cursor(rows[limit - 1][10], rows[limit - 1][0]) if len(rows) > limit else None
    return m.EvidenceList(items=[_evidence(r, jobs[r[0]]) for r in page], next_cursor=nxt)


@router.get("/evidence/{evidence_id}", response_model=m.Evidence)
def get_evidence(evidence_id: str, case: ViewerCase) -> m.Evidence:
    return load_evidence(case.conn, case.case_id, parse_id(evidence_id, "evidence"))


@router.get("/evidence/{evidence_id}/jobs", response_model=m.IngestJobList)
def list_evidence_jobs(evidence_id: str, case: ViewerCase) -> m.IngestJobList:
    ev = load_evidence(case.conn, case.case_id, parse_id(evidence_id, "evidence"))
    return m.IngestJobList(items=ev.ingest_jobs, next_cursor=None)


@router.get("/evidence/{evidence_id}/content", status_code=307)
def get_evidence_content(evidence_id: str, case: ViewerCase, store: StoreDep) -> RedirectResponse:
    eid = parse_id(evidence_id, "evidence")
    row = case.conn.execute(
        "SELECT gcs_uri, sha256 FROM public.evidence WHERE case_id = %s AND id = %s",
        (case.case_id, eid),
    ).fetchone()
    if row is None:
        raise not_found("No such evidence.")
    uri, expected = str(row[0]), str(row[1]).strip()
    try:
        actual: str | None = store.sha256(uri)
    except StorageError:
        actual = None
    ok = actual == expected
    with case.conn.transaction():
        write_audit(
            case.conn,
            case.user.id,
            case.case_id,
            "evidence.view",
            {"evidence_id": str(eid), "sha256": expected, "integrity_ok": ok},
        )
    if not ok:
        raise conflict(
            "The stored file failed its integrity check (SHA-256 mismatch or missing object); "
            "it will not be served.",
            "integrity-check-failed",
        )
    try:
        url = store.signed_url(uri, case.services.settings.signed_url_ttl_s)
    except StorageError as exc:
        raise ProblemError(503, "Could not create a signed URL for the file.") from exc
    return RedirectResponse(url, status_code=307, headers={"Cache-Control": "no-store"})


# ---- upload --------------------------------------------------------------------------------


@lru_cache(maxsize=4)
def first_stage(evidence_type: str) -> str:
    """The modality's first pipeline stage (later stages are queued by the workers)."""
    from caselens_workers.pipeline import load_pipeline

    return load_pipeline(evidence_type).first  # type: ignore[arg-type]


def modality(content_type: str) -> str | None:
    base = content_type.split(";", 1)[0].strip().lower()
    if base == "application/pdf":
        return "pdf"
    if base == "text/csv":
        return "csv"
    if base.startswith("audio/") and len(base) > len("audio/"):
        return "audio"
    if base.startswith("video/") and len(base) > len("video/"):
        return "video"
    return None


def _hash_and_size(data: BinaryIO) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    data.seek(0)
    while chunk := data.read(HASH_CHUNK):
        digest.update(chunk)
        size += len(chunk)
    data.seek(0)
    return digest.hexdigest(), size


def _parse_recorded_at(raw: str | None) -> datetime | None:
    if raw is None or raw == "":
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise validation(
            "recorded_at must be an ISO-8601 date-time.", ["body", "recorded_at"]
        ) from exc
    if value.tzinfo is None:
        raise validation("recorded_at must include a time zone offset.", ["body", "recorded_at"])
    return value


def _store_upload(
    case: ViewerCase,
    store: EvidenceStore,
    upload: UploadFile,
    description: str | None,
    recorded_at: datetime | None,
) -> m.Evidence:
    """Sync part of the upload (runs in the threadpool): hash, dedupe, store, record."""
    conn, user_id, case_id = case.conn, case.user.id, case.case_id
    content_type = (upload.content_type or "").split(";", 1)[0].strip().lower()
    evidence_type = modality(content_type)
    if evidence_type is None:
        raise ProblemError(
            415,
            "Unsupported content type; upload application/pdf, audio/*, video/* or text/csv.",
        )
    filename = os.path.basename((upload.filename or "").replace("\\", "/")).strip()
    if not filename:
        raise validation("The file needs a filename.", ["body", "file"])
    sha256, size = _hash_and_size(upload.file)
    if size > case.services.settings.max_upload_bytes:
        raise ProblemError(413, "The file exceeds the upload size limit.")
    dup = conn.execute(
        "SELECT 1 FROM public.evidence WHERE case_id = %s AND sha256 = %s", (case_id, sha256)
    ).fetchone()
    if dup is not None:
        raise conflict(DUPLICATE, "duplicate-evidence")
    stage = first_stage(evidence_type)
    evidence_id = uuid.uuid4()
    try:
        uri = store.put_stream(evidence_key(case_id, evidence_id), upload.file, size, content_type)
    except StorageError as exc:
        raise ProblemError(503, "The evidence store is unavailable.") from exc
    custody = [
        {
            "at": datetime.now(UTC).isoformat(),
            "action": "uploaded",
            "actor_user_id": str(user_id),
            "sha256": sha256,
            "note": description,
        }
    ]
    try:
        with conn.transaction():
            conn.execute(
                "INSERT INTO public.evidence (id, case_id, type, original_filename, content_type,"
                " size_bytes, gcs_uri, sha256, uploaded_by, recorded_at, chain_of_custody)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    evidence_id,
                    case_id,
                    evidence_type,
                    filename,
                    content_type,
                    size,
                    uri,
                    sha256,
                    user_id,
                    recorded_at,
                    Jsonb(custody),
                ),
            )
            write_audit(
                conn,
                user_id,
                case_id,
                "evidence.upload",
                {
                    "evidence_id": str(evidence_id),
                    "sha256": sha256,
                    "filename": filename,
                    "type": evidence_type,
                    "size_bytes": size,
                },
            )
            conn.execute(
                "INSERT INTO public.ingest_jobs (case_id, evidence_id, stage) VALUES (%s, %s, %s)",
                (case_id, evidence_id, stage),
            )
    except psycopg.errors.UniqueViolation as exc:
        raise conflict(DUPLICATE, "duplicate-evidence") from exc
    return load_evidence(conn, case_id, evidence_id)


@router.post("/evidence", response_model=m.Evidence, status_code=201)
async def upload_evidence(request: Request, case: ViewerCase, store: StoreDep) -> m.Evidence:
    # Role first (403 before any body is read), then size, then the multipart body.
    await run_in_threadpool(require_role, case.conn, case.case_id, "analyst")
    limit = case.services.settings.max_upload_bytes
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit + 64 * 1024:
        raise ProblemError(413, "The file exceeds the upload size limit.")
    content_type = request.headers.get("content-type", "")
    if not content_type.lower().startswith("multipart/form-data"):
        raise ProblemError(415, "The request body must be multipart/form-data.")
    async with request.form(max_files=1, max_fields=4, max_part_size=MAX_DESCRIPTION * 4) as form:
        unknown = sorted(set(form.keys()) - {"file", "description", "recorded_at"})
        if unknown:
            raise validation(f"Unknown form field: {unknown[0]}.", ["body", unknown[0]])
        upload = form.get("file")
        if not isinstance(upload, UploadFile):
            raise validation("The file field is required.", ["body", "file"])
        description = form.get("description")
        recorded_raw = form.get("recorded_at")
        if description is not None and not isinstance(description, str):
            raise validation("description must be text.", ["body", "description"])
        if recorded_raw is not None and not isinstance(recorded_raw, str):
            raise validation("recorded_at must be text.", ["body", "recorded_at"])
        if description is not None and len(description) > MAX_DESCRIPTION:
            raise validation("description is too long.", ["body", "description"])
        recorded_at = _parse_recorded_at(recorded_raw)
        return await run_in_threadpool(
            _store_upload, case, store, upload, description or None, recorded_at
        )
