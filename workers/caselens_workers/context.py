"""What a stage gets to work with: the job, its evidence row, the DB, and object storage."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from caselens_workers.db import Conn
from caselens_workers.jobs import JobMessage
from caselens_workers.settings import WorkerSettings
from caselens_workers.storage import ObjectStore, artifact_key, split_uri

EvidenceType = Literal["pdf", "audio", "video", "csv"]


class IntegrityError(RuntimeError):
    """The stored original no longer matches the sha256 recorded at upload."""


@dataclass(frozen=True)
class Evidence:
    id: uuid.UUID
    case_id: uuid.UUID
    type: EvidenceType
    original_filename: str
    content_type: str
    gcs_uri: str
    sha256: str
    recorded_at: datetime | None


def load_evidence(conn: Conn, evidence_id: uuid.UUID) -> Evidence:
    row = conn.execute(
        "SELECT id, case_id, type, original_filename, content_type, gcs_uri, sha256, recorded_at "
        "FROM public.evidence WHERE id = %s",
        (evidence_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"evidence {evidence_id} not found")
    return Evidence(*row)


@dataclass
class StageResult:
    """Optional stage output: a different next stage (e.g. PDF parse -> render for scans) and a
    small summary that goes into the stage's audit event."""

    next_stage: str | None = None
    summary: dict[str, Any] = field(default_factory=dict)


@dataclass
class StageContext:
    job: JobMessage
    evidence: Evidence
    conn: Conn
    store: ObjectStore
    settings: WorkerSettings
    workdir: Path

    def original_path(self) -> Path:
        """Download the original once per job and re-verify its sha256 (evidence integrity)."""
        suffix = Path(self.evidence.original_filename).suffix
        path = self.workdir / f"original{suffix}"
        if not path.exists():
            _, key = split_uri(self.evidence.gcs_uri)
            self.store.download(key, path)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != self.evidence.sha256:
                raise IntegrityError(
                    f"sha256 mismatch for evidence {self.evidence.id}: "
                    f"expected {self.evidence.sha256}, got {digest}"
                )
        return path

    def _key(self, stage: str, name: str) -> str:
        return artifact_key(self.evidence.case_id, self.evidence.id, stage, name)

    def put_artifact(self, name: str, data: bytes, content_type: str) -> str:
        """Store a derived artifact under this stage's deterministic key."""
        return self.store.put_bytes(self._key(self.job.stage, name), data, content_type)

    def get_artifact(self, stage: str, name: str) -> bytes:
        return self.store.get_bytes(self._key(stage, name))

    def put_json(self, name: str, value: Any) -> str:
        data = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return self.put_artifact(name, data, "application/json")

    def get_json(self, stage: str, name: str) -> Any:
        return json.loads(self.get_artifact(stage, name))
