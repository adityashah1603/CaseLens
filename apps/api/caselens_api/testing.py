"""Test helpers for the API (SYNTHETIC data only): a contract checker that validates response
bodies against `docs/contracts/openapi.yaml` (JSON Schema 2020-12, $refs resolved) and a seeded
SYNTHETIC world (users, two cases, evidence, chunks, entities, links, events, a conflict).

Used by `apps/api/tests`; never imported by the request path.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any, LiteralString

import httpx
import jsonschema
import psycopg
import yaml
from psycopg.types.json import Jsonb
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from caselens_api.auth import DevTokenVerifier
from caselens_api.storage import MemoryEvidenceStore
from caselens_workers.settings import libpq_url
from caselens_workers.storage import evidence_key

ROOT = Path(__file__).resolve().parents[3]
OPENAPI = ROOT / "docs" / "contracts" / "openapi.yaml"
DEV_SECRET = "SYNTHETIC-test-secret-0123456789abcdef"  # noqa: S105 - test-only, never deployed
PDF_BYTES = b"%PDF-1.4\n% SYNTHETIC test evidence for the CaseLens API tests\n%%EOF\n"
STATEMENT = "SYNTHETIC. Dale Mercer said he drove the gray sedan SYN4821 into Riverside Lot."


# ---- contract checker ----------------------------------------------------------------------


@cache
def _spec() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(OPENAPI.read_text(encoding="utf-8"))
    return loaded


@cache
def _registry() -> Registry[Any]:
    resource: Resource[Any] = Resource.from_contents(_spec(), default_specification=DRAFT202012)
    registry: Registry[Any] = Registry().with_resource("urn:caselens:openapi", resource)
    return registry


def schema_errors(instance: Any, schema_name: str) -> list[str]:
    validator = jsonschema.Draft202012Validator(
        {"$ref": f"urn:caselens:openapi#/components/schemas/{schema_name}"},
        registry=_registry(),
        format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER,
    )
    return [
        f"{'/'.join(str(p) for p in e.absolute_path) or '(root)'}: {e.message[:300]}"
        for e in validator.iter_errors(instance)
    ]


def _expect(condition: bool, message: object) -> None:
    if not condition:
        raise AssertionError(message)


def check(response: httpx.Response, status: int, schema_name: str | None = None) -> Any:
    """Check the status, then validate the body against the contract: `schema_name` for 2xx
    JSON, `Problem` (with application/problem+json) for every error. Raises AssertionError."""
    _expect(response.status_code == status, (response.status_code, response.text[:800]))
    if status >= 400:
        content_type = response.headers.get("content-type", "")
        _expect(content_type.startswith("application/problem+json"), content_type)
        body = response.json()
        _expect(schema_errors(body, "Problem") == [], body)
        _expect(body["status"] == status, body)
        return body
    if schema_name is None:
        return None
    body = response.json()
    errors = schema_errors(body, schema_name)
    _expect(errors == [], errors[:10])
    return body


# ---- SYNTHETIC world -----------------------------------------------------------------------


@dataclass
class World:
    url: str
    store: MemoryEvidenceStore
    users: dict[str, uuid.UUID]
    case_id: uuid.UUID
    other_case_id: uuid.UUID
    evidence_id: uuid.UUID
    chunk_id: uuid.UUID
    entities: dict[str, uuid.UUID]
    link_id: uuid.UUID
    tokens: dict[str, str] = field(default_factory=dict)

    def auth(self, who: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tokens[who]}"}


def seed_world(url: str, store: MemoryEvidenceStore) -> World:
    tag = uuid.uuid4().hex[:8]
    users = {k: uuid.uuid4() for k in ("lead", "analyst", "viewer", "outsider", "admin", "demo")}
    case_id, other_case_id, evidence_id, chunk_id = (uuid.uuid4() for _ in range(4))
    entities = {k: uuid.uuid4() for k in ("mercer", "dale", "sedan", "lot")}
    link_id = uuid.uuid4()
    with psycopg.connect(libpq_url(url), autocommit=True) as conn, conn.transaction():
        for key, uid in users.items():
            conn.execute(
                "INSERT INTO public.users (id, oidc_subject, email, display_name, is_demo, "
                "is_admin) VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    uid,
                    f"test:{tag}:{key}",
                    f"{key}.{tag}@example.test",
                    f"SYNTHETIC {key}",
                    key == "demo",
                    key == "admin",
                ),
            )
        for cid, lead, name in (
            (case_id, "lead", f"SYNTHETIC Riverside Lot {tag}"),
            (other_case_id, "outsider", f"SYNTHETIC Harbor Street {tag}"),
        ):
            conn.execute(
                "INSERT INTO public.cases (id, name, created_by) VALUES (%s, %s, %s)",
                (cid, name, users[lead]),
            )
        for cid, who, role in (
            (case_id, "lead", "lead"),
            (case_id, "analyst", "analyst"),
            (case_id, "viewer", "viewer"),
            (case_id, "demo", "viewer"),
            (other_case_id, "outsider", "lead"),
        ):
            conn.execute(
                "INSERT INTO public.case_members (case_id, user_id, role) VALUES (%s, %s, %s)",
                (cid, users[who], role),
            )
        key = evidence_key(case_id, evidence_id)
        uri = store.store.put_bytes(key, PDF_BYTES, "application/pdf")
        sha = hashlib.sha256(PDF_BYTES).hexdigest()
        conn.execute(
            "INSERT INTO public.evidence (id, case_id, type, original_filename, content_type, "
            "size_bytes, gcs_uri, sha256, uploaded_by, chain_of_custody) "
            "VALUES (%s, %s, 'pdf', 'statement.pdf', 'application/pdf', %s, %s, %s, %s, %s)",
            (
                evidence_id,
                case_id,
                len(PDF_BYTES),
                uri,
                sha,
                users["lead"],
                Jsonb(
                    [
                        {
                            "at": "2026-03-20T09:00:00+00:00",
                            "action": "uploaded",
                            "actor_user_id": str(users["lead"]),
                            "sha256": sha,
                            "note": "SYNTHETIC",
                        }
                    ]
                ),
            ),
        )
        for stage, created in (
            ("parse", "2026-03-20T09:00:01Z"),
            ("chunk", "2026-03-20T09:00:02Z"),
        ):
            conn.execute(
                "INSERT INTO public.ingest_jobs (case_id, evidence_id, stage, status, attempts, "
                "created_at, updated_at) VALUES (%s, %s, %s, 'succeeded', 1, %s, %s)",
                (case_id, evidence_id, stage, created, created),
            )
        conn.execute(
            "INSERT INTO public.chunks (id, case_id, evidence_id, modality, chunk_index, text, "
            "locator) VALUES (%s, %s, %s, 'pdf', 0, %s, %s)",
            (
                chunk_id,
                case_id,
                evidence_id,
                STATEMENT,
                Jsonb({"kind": "pdf", "page": 2, "bbox": [72.0, 410.5, 540.0, 452.0]}),
            ),
        )
        for key, etype, name, norm, attrs in (
            ("mercer", "person", "Dale Mercer", "dale mercer", {"aliases": ["D. Mercer"]}),
            ("dale", "person", "Dale", "dale", {}),
            ("sedan", "vehicle", "SYN4821", "SYN4821", {"color": "gray"}),
            ("lot", "location", "Riverside Lot", "riverside lot", {}),
        ):
            conn.execute(
                "INSERT INTO public.entities (id, case_id, type, canonical_name, normalized_key, "
                "attributes) VALUES (%s, %s, %s, %s, %s, %s)",
                (entities[key], case_id, etype, name, norm, Jsonb(attrs)),
            )
        conn.execute(
            "INSERT INTO public.entity_links (id, case_id, from_entity_id, to_entity_id, method, "
            "confidence) VALUES (%s, %s, %s, %s, 'fuzzy', 0.9)",
            (link_id, case_id, entities["dale"], entities["mercer"]),
        )
        for key, surface in (("mercer", "Dale Mercer"), ("sedan", "SYN4821"), ("lot", "lot")):
            conn.execute(
                "INSERT INTO public.mentions (case_id, entity_id, chunk_id, surface_text, "
                "confidence) VALUES (%s, %s, %s, %s, 0.9)",
                (case_id, entities[key], chunk_id, surface),
            )
        for t, loc, chunks, desc in (
            ("2026-03-14T22:41:00-07:00", None, [chunk_id], "SYNTHETIC: Mercer drives in."),
            ("2026-03-14T22:47:00-07:00", entities["lot"], [], "SYNTHETIC: sedan leaves."),
            ("2026-03-10T09:00:00-07:00", None, [], "SYNTHETIC: unrelated event."),
        ):
            conn.execute(
                "INSERT INTO public.events (case_id, t_start, location_id, description, "
                "source_chunk_ids, confidence) VALUES (%s, %s, %s, %s, %s, 0.8)",
                (case_id, t, loc, desc, chunks),
            )
        ref = {
            "source_type": "chunk",
            "evidence_id": str(evidence_id),
            "chunk_id": str(chunk_id),
            "locator": {"kind": "pdf", "page": 2, "bbox": None, "ocr_confidence": None},
        }
        conn.execute(
            "INSERT INTO public.conflicts (case_id, description, claim_a, claim_b) "
            "VALUES (%s, 'SYNTHETIC driver conflict', %s, %s)",
            (
                case_id,
                Jsonb({"text": "Mercer drove.", "source_refs": [ref]}),
                Jsonb({"text": "Someone else drove.", "source_refs": [ref]}),
            ),
        )
    verifier = DevTokenVerifier(DEV_SECRET)
    world = World(
        url, store, users, case_id, other_case_id, evidence_id, chunk_id, entities, link_id
    )
    world.tokens = {k: verifier.mint(f"test:{tag}:{k}") for k in users}
    world.tokens["newcomer"] = verifier.mint(f"test:{tag}:newcomer", email=f"new.{tag}@x.test")
    return world


def owner_rows(url: str, query: LiteralString, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
    """Read back rows as the owner (bypasses RLS), for asserting audit writes."""
    with psycopg.connect(libpq_url(url)) as conn:
        return conn.execute(query, params).fetchall()
