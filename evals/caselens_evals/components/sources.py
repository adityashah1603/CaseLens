"""Read-only access to the ingested case: chunks, entities and links (Postgres) and stage
artifacts (MinIO). Ground truth comes from data_gen and never flows the other way."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from caselens_workers.db import Conn
from caselens_workers.storage import ObjectStore, artifact_key

DATA_GEN_OUT = Path(__file__).resolve().parents[3] / "data_gen" / "output"


@dataclass(frozen=True)
class Chunk:
    index: int
    text: str
    locator: dict[str, Any]


class CaseSource:
    """One case's ingested data. Every query is a parameterized SELECT on a read-only session."""

    def __init__(self, conn: Conn, store: ObjectStore | None, case_id: uuid.UUID) -> None:
        self.conn = conn
        self.store = store
        self.case_id = case_id
        rows = conn.execute(
            "SELECT original_filename, id FROM public.evidence WHERE case_id = %s", (case_id,)
        ).fetchall()
        self.evidence: dict[str, uuid.UUID] = {r[0]: r[1] for r in rows}

    def evidence_id(self, filename: str) -> uuid.UUID:
        if filename not in self.evidence:
            raise LookupError(f"{filename} is not ingested in case {self.case_id}")
        return self.evidence[filename]

    def chunks(self, filename: str) -> list[Chunk]:
        rows = self.conn.execute(
            "SELECT chunk_index, text, locator FROM public.chunks WHERE evidence_id = %s "
            "ORDER BY chunk_index",
            (self.evidence_id(filename),),
        ).fetchall()
        return [Chunk(index=r[0], text=r[1], locator=r[2]) for r in rows]

    def artifact_json(self, filename: str, stage: str, name: str) -> Any | None:
        """A stage's JSON artifact, or None when there is no object store or no such object."""
        if self.store is None:
            return None
        key = artifact_key(self.case_id, self.evidence_id(filename), stage, name)
        try:
            if not self.store.exists(key):
                return None
            return json.loads(self.store.get_bytes(key))
        except Exception:  # missing bucket / object / bad JSON: report it as absent
            return None

    def entities(self) -> list[tuple[str, str, str, str | None, dict[str, Any]]]:
        """(id, type, canonical_name, normalized_key, attributes) per entity."""
        rows = self.conn.execute(
            "SELECT id, type, canonical_name, normalized_key, attributes FROM public.entities "
            "WHERE case_id = %s ORDER BY type, canonical_name",
            (self.case_id,),
        ).fetchall()
        return [(str(r[0]), r[1], r[2], r[3], r[4] or {}) for r in rows]

    def active_links(self) -> list[tuple[str, str, str]]:
        """(from_entity_id, to_entity_id, method) per active link."""
        rows = self.conn.execute(
            "SELECT from_entity_id, to_entity_id, method FROM public.entity_links "
            "WHERE case_id = %s AND status = 'active'",
            (self.case_id,),
        ).fetchall()
        return [(str(r[0]), str(r[1]), r[2]) for r in rows]


def find_case(conn: Conn, name: str) -> uuid.UUID:
    row = conn.execute("SELECT id FROM public.cases WHERE name = %s", (name,)).fetchone()
    if row is None:
        raise SystemExit(f"case {name!r} not found")
    case_id: uuid.UUID = row[0]
    return case_id
