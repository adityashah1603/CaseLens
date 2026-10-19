"""Dev seeding: upload a generated SYNTHETIC case (data_gen output) the way the API upload will.

Runs as `caselens_app` with `app.user_id` set, so RLS and column grants apply exactly as in the
request path: bootstrap the lead's membership, store each original, insert `evidence` with its
first custody entry, write `evidence.upload` to the audit log, and enqueue the first stage.
Re-running is safe: files already in the case (same sha256) are skipped.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from caselens_workers.context import EvidenceType
from caselens_workers.db import Conn
from caselens_workers.storage import ObjectStore, evidence_key

SEED_SUBJECT = "local-dev:seed-lead"
_CONTENT_TYPES = {".csv": "text/csv", ".pdf": "application/pdf", ".wav": "audio/wav"}


@dataclass
class SubmitReport:
    case_id: uuid.UUID
    uploaded: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def ensure_user(conn: Conn, subject: str, display_name: str) -> uuid.UUID:
    row = conn.execute("SELECT id FROM public.users WHERE oidc_subject = %s", (subject,)).fetchone()
    if row is not None:
        user_id: uuid.UUID = row[0]
    else:
        user_id = uuid.uuid4()
        conn.execute(
            "INSERT INTO public.users (id, oidc_subject, display_name) VALUES (%s, %s, %s)",
            (user_id, subject, display_name),
        )
    conn.execute("SELECT set_config('app.user_id', %s, false)", (str(user_id),))
    return user_id


def ensure_case(conn: Conn, user_id: uuid.UUID, name: str, description: str) -> uuid.UUID:
    row = conn.execute("SELECT id FROM public.cases WHERE name = %s", (name,)).fetchone()
    if row is not None:
        existing: uuid.UUID = row[0]
        return existing
    case_id = uuid.uuid4()  # generated client-side: no RETURNING before membership (D-026)
    with conn.transaction():
        conn.execute(
            "INSERT INTO public.cases (id, name, description, created_by) VALUES (%s, %s, %s, %s)",
            (case_id, name, description, user_id),
        )
        conn.execute(
            "INSERT INTO public.case_members (case_id, user_id, role) VALUES (%s, %s, 'lead')",
            (case_id, user_id),
        )
        conn.execute(
            "INSERT INTO public.audit_log (user_id, case_id, action, payload) "
            "VALUES (%s, %s, 'case.create', %s)",
            (user_id, case_id, Jsonb({"name": name})),
        )
    return case_id


def submit_file(
    conn: Conn,
    store: ObjectStore,
    user_id: uuid.UUID,
    case_id: uuid.UUID,
    path: Path,
    evidence_type: EvidenceType,
    first_stage: str,
    recorded_at: str | None,
) -> bool:
    """Upload one original. Returns False if the case already holds a file with this sha256."""
    sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    dup = conn.execute(
        "SELECT 1 FROM public.evidence WHERE case_id = %s AND sha256 = %s", (case_id, sha256)
    ).fetchone()
    if dup is not None:
        return False
    evidence_id = uuid.uuid4()
    content_type = _CONTENT_TYPES.get(path.suffix.lower()) or (
        mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    )
    uri = store.put_file(evidence_key(case_id, evidence_id), path, content_type)
    custody: list[dict[str, Any]] = [
        {
            "at": datetime.now(UTC).isoformat(),
            "action": "uploaded",
            "actor_user_id": str(user_id),
            "sha256": sha256,
            "note": "SYNTHETIC demo evidence seeded from data_gen output",
        }
    ]
    with conn.transaction():
        conn.execute(
            "INSERT INTO public.evidence (id, case_id, type, original_filename, content_type, "
            "size_bytes, gcs_uri, sha256, uploaded_by, recorded_at, chain_of_custody) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                evidence_id,
                case_id,
                evidence_type,
                path.name,
                content_type,
                path.stat().st_size,
                uri,
                sha256,
                user_id,
                recorded_at,
                Jsonb(custody),
            ),
        )
        conn.execute(
            "INSERT INTO public.audit_log (user_id, case_id, action, payload) "
            "VALUES (%s, %s, 'evidence.upload', %s)",
            (
                user_id,
                case_id,
                Jsonb({"evidence_id": str(evidence_id), "sha256": sha256, "filename": path.name}),
            ),
        )
        conn.execute(
            "INSERT INTO public.ingest_jobs (case_id, evidence_id, stage) VALUES (%s, %s, %s)",
            (case_id, evidence_id, first_stage),
        )
    return True


def submit_case(
    conn: Conn,
    store: ObjectStore,
    case_dir: Path,
    case_name: str,
    first_stages: dict[EvidenceType, str],
    only_types: set[str] | None = None,
) -> SubmitReport:
    """Seed one generated case directory (needs its manifest.json)."""
    manifest = json.loads((case_dir / "manifest.json").read_text(encoding="utf-8"))
    user_id = ensure_user(conn, SEED_SUBJECT, "Seed lead (SYNTHETIC)")
    case_id = ensure_case(conn, user_id, case_name, manifest["notice"])
    report = SubmitReport(case_id)
    for entry in manifest["files"]:
        if only_types is not None and entry["type"] not in only_types:
            continue
        path = case_dir / entry["path"]
        ok = submit_file(
            conn,
            store,
            user_id,
            case_id,
            path,
            entry["type"],
            first_stages[entry["type"]],
            entry.get("recorded_at"),
        )
        (report.uploaded if ok else report.skipped).append(entry["path"])
    return report
