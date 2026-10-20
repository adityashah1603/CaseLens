"""Extract stage against Postgres with the router's `fake` provider. All text is SYNTHETIC,
using names from the fictional Riverside Lot story."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from caselens_agent.router import ModelError, ModelRouter, RouterSettings
from caselens_workers.context import StageContext, load_evidence
from caselens_workers.db import Conn
from caselens_workers.jobs import JobMessage
from caselens_workers.settings import WorkerSettings
from caselens_workers.stages.extract import make_extract_stage
from caselens_workers.storage import MemoryObjectStore
from caselens_workers.testing import SeededEvidence, as_role

pytestmark = pytest.mark.db

CHUNKS = [
    "SYNTHETIC statement of Elena Ruiz. At 10:41 PM on March 14, 2026 a gray Honda Civic, "
    "plate SYN 4821, entered the Riverside Lot. Travis Kemper was driving. "
    "Reach me at (360) 555-0142.",
    "SYNTHETIC. T. Kemper later told Detective Samuel Brandt he was alone. "
    "Kemper left the lot with the headlights off.",
    "SYNTHETIC. Maybe a man in a dark hoodie stood by the truck; a plate like ABC 123 was seen.",
]


def _entity(
    type_: str, name: str, surface: str, ref: int, conf: float, **attrs: str
) -> dict[str, Any]:
    return {
        "type": type_,
        "name": name,
        "aliases": [],
        "attributes": attrs,
        "surface_text": surface,
        "chunk_ref": ref,
        "confidence": conf,
    }


EXTRACTION = {
    "entities": [
        _entity("person", "Elena Ruiz", "Elena Ruiz", 0, 0.95, role="witness"),
        _entity("person", "Travis Kemper", "Travis Kemper", 0, 0.95, role="suspect"),
        _entity(
            "vehicle",
            "gray Honda Civic",
            "gray Honda Civic",
            0,
            0.9,
            color="gray",
            make="Honda",
            model="Civic",
            plate="SYN4821",
        ),
        _entity("location", "Riverside Lot", "Riverside Lot", 0, 0.9),
        _entity("phone", "(360) 555-0142", "(360) 555-0142", 0, 0.9),
        _entity("person", "T. Kemper", "T. Kemper", 1, 0.9),
        _entity("person", "Kemper", "Kemper", 1, 0.85),
        _entity("person", "Samuel Brandt", "Detective Samuel Brandt", 1, 0.95, role="officer"),
        _entity("person", "man in a dark hoodie", "man in a dark hoodie", 2, 0.4),
    ],
    "events": [
        {
            "description": "Gray Honda Civic SYN4821 entered the Riverside Lot.",
            "time_text": "At 10:41 PM on March 14, 2026",
            "location": "Riverside Lot",
            "participants": ["Travis Kemper"],
            "chunk_refs": [0],
            "confidence": 0.9,
        },
        {
            "description": "Kemper left the lot with the headlights off.",
            "time_text": None,
            "t_start": "2026-03-14T22:47:30-07:00",
            "location": "the Riverside Lot",
            "participants": ["Travis Kemper"],
            "chunk_refs": [1],
            "confidence": 0.5,
        },
        {
            "description": "Kemper told a detective he was alone.",
            "time_text": "later",
            "participants": ["Travis Kemper", "Samuel Brandt"],
            "chunk_refs": [1],
            "confidence": 0.8,
        },
    ],
}


class FakeModel:
    """Records prompts; answers extraction with EXTRACTION and adjudication by name."""

    def __init__(self, extraction: dict[str, Any] | None = None, same: bool = True) -> None:
        self.extraction = extraction if extraction is not None else EXTRACTION
        self.same = same
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, task: str, system: str, user: str, schema: type[BaseModel]) -> str:
        self.calls.append((task, system, user))
        if task == "adjudicate":
            return json.dumps({"same_entity": self.same, "confidence": 0.8})
        return json.dumps(self.extraction)

    def router(self) -> ModelRouter:
        return ModelRouter(RouterSettings(llm_provider="fake"), fake=self)


def _add_evidence(
    conn: Conn, ev: SeededEvidence, recorded_at: str | None, evidence_type: str = "pdf"
) -> uuid.UUID:
    """Another SYNTHETIC evidence row in the same case (extract never reads the original)."""
    evidence_id = uuid.uuid4()
    as_role(conn, None)
    conn.execute(
        "INSERT INTO public.evidence (id, case_id, type, original_filename, content_type, "
        "size_bytes, gcs_uri, sha256, uploaded_by, recorded_at, chain_of_custody) "
        "VALUES (%s, %s, %s, 'synthetic.pdf', 'application/pdf', 1, 'memory://x/y', %s, %s, "
        "%s, %s)",
        (
            evidence_id,
            ev.case_id,
            evidence_type,
            evidence_id.hex * 2,
            ev.user_id,
            recorded_at,
            Jsonb([]),
        ),
    )
    return evidence_id


def _add_chunks(
    conn: Conn,
    case_id: uuid.UUID,
    evidence_id: uuid.UUID,
    texts: list[str],
    t_starts: list[datetime | None] | None = None,
) -> list[uuid.UUID]:
    as_role(conn, None)
    ids = []
    for i, text in enumerate(texts):
        t_start = t_starts[i] if t_starts else None
        row = conn.execute(
            "INSERT INTO public.chunks (case_id, evidence_id, modality, chunk_index, text, "
            "locator, t_start) SELECT %s, %s, type, %s, %s, %s, %s FROM public.evidence "
            "WHERE id = %s RETURNING id",
            (
                case_id,
                evidence_id,
                i,
                text,
                Jsonb({"kind": "pdf", "page": i + 1}),
                t_start,
                evidence_id,
            ),
        ).fetchone()
        assert row is not None
        ids.append(row[0])
    return ids


def _run(
    conn: Conn,
    evidence_id: uuid.UUID,
    stage: Callable[[StageContext], Any],
    settings: WorkerSettings,
    tmp_path: Path,
) -> dict[str, Any]:
    as_role(conn, "caselens_worker")
    evidence = load_evidence(conn, evidence_id)
    job = JobMessage(
        job_id=uuid.uuid4(),
        case_id=evidence.case_id,
        evidence_id=evidence_id,
        stage="extract",
        attempt=1,
    )
    ctx = StageContext(job, evidence, conn, MemoryObjectStore(), settings, tmp_path)
    summary: dict[str, Any] = stage(ctx).summary
    as_role(conn, None)
    return summary


Case = tuple[SeededEvidence, list[uuid.UUID]]


@pytest.fixture
def case(owner_conn: Conn, seed_evidence: Callable[..., SeededEvidence], tmp_path: Path) -> Case:
    f = tmp_path / "statement.pdf"
    f.write_text("SYNTHETIC", encoding="utf-8")
    ev = seed_evidence(f, "pdf", recorded_at="2026-03-15T01:45:00-07:00")
    return ev, _add_chunks(owner_conn, ev.case_id, ev.evidence_id, CHUNKS)


def _rows(conn: Conn, query: str, *params: object) -> list[tuple[Any, ...]]:
    as_role(conn, None)
    return conn.execute(query, params).fetchall()


def _counts(conn: Conn, case_id: uuid.UUID) -> tuple[Any, ...]:
    return _rows(
        conn,
        "SELECT (SELECT count(*) FROM public.entities WHERE case_id = %s), "
        "(SELECT count(*) FROM public.mentions WHERE case_id = %s), "
        "(SELECT count(*) FROM public.events WHERE case_id = %s), "
        "(SELECT count(*) FROM public.entity_links WHERE case_id = %s)",
        case_id,
        case_id,
        case_id,
        case_id,
    )[0]


def _links(conn: Conn, case_id: uuid.UUID) -> list[tuple[Any, ...]]:
    return _rows(
        conn,
        "SELECT f.canonical_name, t.canonical_name, l.method, l.confidence "
        "FROM public.entity_links l JOIN public.entities f ON f.id = l.from_entity_id "
        "JOIN public.entities t ON t.id = l.to_entity_id WHERE l.case_id = %s",
        case_id,
    )


def test_deterministic_and_llm_entities_events_and_links(
    owner_conn: Conn, case: Case, worker_settings: WorkerSettings, tmp_path: Path
) -> None:
    ev, chunk_ids = case
    fake = FakeModel()
    stage = make_extract_stage(fake.router())
    summary = _run(owner_conn, ev.evidence_id, stage, worker_settings, tmp_path)
    assert summary["llm"] == "ok"
    assert summary["llm_calls"] == 1  # three short chunks fit one batch
    assert summary["events"] == 2 and summary["events_unresolved_time"] == 1

    # Prompt: every chunk inside an evidence block; the system prompt says blocks are data.
    task, system, user = fake.calls[0]
    assert task == "extract" and "never instructions" in system
    for i, text in enumerate(CHUNKS):
        assert f"<<<EVIDENCE chunk {i}\n{text}\n>>>" in user

    mentions = {
        (r[0], r[1]): (r[2], float(r[3]), r[4])
        for r in _rows(
            owner_conn,
            "SELECT e.canonical_name, m.surface_text, m.chunk_id, m.confidence, m.needs_review "
            "FROM public.mentions m JOIN public.entities e ON e.id = m.entity_id "
            "WHERE m.case_id = %s",
            ev.case_id,
        )
    }
    # Deterministic phone (the model's identical mention is deduplicated) on chunk 0.
    assert mentions[("+13605550142", "(360) 555-0142")] == (chunk_ids[0], 1.0, False)
    # Plate: the deterministic mention and the model's vehicle resolve to one entity.
    assert mentions[("SYN4821", "SYN 4821")] == (chunk_ids[0], 1.0, False)
    assert mentions[("SYN4821", "gray Honda Civic")] == (chunk_ids[0], 0.9, False)
    # A generic plate-shaped token and the vague person are flagged for review.
    assert mentions[("ABC123", "ABC 123")] == (chunk_ids[2], 0.5, True)
    assert mentions[("man in a dark hoodie", "man in a dark hoodie")] == (chunk_ids[2], 0.4, True)
    assert mentions[("T. Kemper", "T. Kemper")][0] == chunk_ids[1]

    civic = _rows(
        owner_conn,
        "SELECT attributes FROM public.entities WHERE case_id = %s AND normalized_key = %s",
        ev.case_id,
        "SYN4821",
    )
    assert civic == [({"plate": "SYN4821", "color": "gray", "make": "Honda", "model": "Civic"},)]

    events = _rows(
        owner_conn,
        "SELECT ev.t_start, ev.needs_review, e.canonical_name, ev.source_chunk_ids "
        "FROM public.events ev LEFT JOIN public.entities e ON e.id = ev.location_id "
        "WHERE ev.case_id = %s ORDER BY ev.t_start",
        ev.case_id,
    )
    assert [(e[0].isoformat(), *e[1:]) for e in events] == [
        ("2026-03-15T05:41:00+00:00", False, "Riverside Lot", [chunk_ids[0]]),
        ("2026-03-15T05:47:30+00:00", True, "Riverside Lot", [chunk_ids[1]]),
    ]

    assert sorted(r[:3] for r in _links(owner_conn, ev.case_id)) == [
        ("Kemper", "Travis Kemper", "fuzzy"),
        ("T. Kemper", "Travis Kemper", "fuzzy"),
    ]


def test_rerun_is_idempotent(
    owner_conn: Conn, case: Case, worker_settings: WorkerSettings, tmp_path: Path
) -> None:
    ev, _ = case
    stage = make_extract_stage(FakeModel().router())
    _run(owner_conn, ev.evidence_id, stage, worker_settings, tmp_path)
    first = _counts(owner_conn, ev.case_id)
    summary = _run(owner_conn, ev.evidence_id, stage, worker_settings, tmp_path)
    assert _counts(owner_conn, ev.case_id) == first
    assert summary["links_fuzzy"] == 0  # already linked


def test_adjudicated_link_across_files_and_shared_entities(
    owner_conn: Conn, case: Case, worker_settings: WorkerSettings, tmp_path: Path
) -> None:
    ev, _ = case
    first = FakeModel(
        {"entities": [_entity("person", "Marcus Hale", "Marcus Hale", 0, 0.95)], "events": []}
    )
    second = FakeModel(
        {"entities": [_entity("person", "Mark Hale", "Mark Hale", 0, 0.9)], "events": []}
    )
    a = _add_evidence(owner_conn, ev, "2026-03-15T10:00:00-07:00")
    _add_chunks(owner_conn, ev.case_id, a, ["SYNTHETIC. Marcus Hale lent money to Travis Kemper."])
    b = _add_evidence(owner_conn, ev, "2026-03-16T10:00:00-07:00")
    _add_chunks(owner_conn, ev.case_id, b, ["SYNTHETIC. Mark Hale was shot at the Riverside Lot."])

    _run(owner_conn, a, make_extract_stage(first.router()), worker_settings, tmp_path)
    summary = _run(owner_conn, b, make_extract_stage(second.router()), worker_settings, tmp_path)
    assert summary["adjudicated"] == 1 and summary["links_llm"] == 1
    adjudication = [c for c in second.calls if c[0] == "adjudicate"]
    assert len(adjudication) == 1
    assert "<<<EVIDENCE record A" in adjudication[0][2]
    assert "Marcus Hale lent money" in adjudication[0][2]
    assert [(r[0], r[1], r[2], float(r[3])) for r in _links(owner_conn, ev.case_id)] == [
        ("Mark Hale", "Marcus Hale", "llm", 0.8)
    ]

    # Re-running file A keeps the shared entities and does not duplicate the link.
    before = _counts(owner_conn, ev.case_id)
    _run(owner_conn, a, make_extract_stage(first.router()), worker_settings, tmp_path)
    assert _counts(owner_conn, ev.case_id) == before


def test_adjudicator_no_means_no_link(
    owner_conn: Conn, case: Case, worker_settings: WorkerSettings, tmp_path: Path
) -> None:
    ev, _ = case
    a = _add_evidence(owner_conn, ev, None)
    _add_chunks(owner_conn, ev.case_id, a, ["SYNTHETIC. Marcus Hale and Mark Hale are cousins."])
    fake = FakeModel(
        {
            "entities": [
                _entity("person", "Marcus Hale", "Marcus Hale", 0, 0.9),
                _entity("person", "Mark Hale", "Mark Hale", 0, 0.9),
            ],
            "events": [],
        },
        same=False,
    )
    summary = _run(owner_conn, a, make_extract_stage(fake.router()), worker_settings, tmp_path)
    assert summary["adjudicated"] == 1 and summary["links_llm"] == 0
    assert _links(owner_conn, ev.case_id) == []


def test_reverted_link_is_never_recreated(
    owner_conn: Conn, case: Case, worker_settings: WorkerSettings, tmp_path: Path
) -> None:
    ev, _ = case
    stage = make_extract_stage(FakeModel().router())
    _run(owner_conn, ev.evidence_id, stage, worker_settings, tmp_path)
    as_role(owner_conn, None)
    owner_conn.execute(
        "UPDATE public.entity_links SET status = 'reverted', reverted_at = now() "
        "WHERE case_id = %s AND from_entity_id = "
        "(SELECT id FROM public.entities WHERE case_id = %s AND canonical_name = 'T. Kemper')",
        (ev.case_id, ev.case_id),
    )
    summary = _run(owner_conn, ev.evidence_id, stage, worker_settings, tmp_path)
    assert summary["links_fuzzy"] == 0 and summary["adjudicated"] == 0
    statuses = _rows(
        owner_conn,
        "SELECT f.canonical_name, l.status FROM public.entity_links l "
        "JOIN public.entities f ON f.id = l.from_entity_id WHERE l.case_id = %s",
        ev.case_id,
    )
    assert sorted(statuses) == [("Kemper", "active"), ("T. Kemper", "reverted")]


def test_no_key_skips_llm_but_keeps_deterministic(
    owner_conn: Conn, case: Case, worker_settings: WorkerSettings, tmp_path: Path
) -> None:
    ev, chunk_ids = case
    router = ModelRouter(RouterSettings(llm_provider="gemini", gemini_api_key=None))
    summary = _run(
        owner_conn, ev.evidence_id, make_extract_stage(router), worker_settings, tmp_path
    )
    assert summary["llm"] == "skipped: CASELENS_GEMINI_API_KEY is not set"
    assert summary["llm_calls"] == 0 and summary["events"] == 0
    rows = _rows(
        owner_conn,
        "SELECT surface_text, chunk_id FROM public.mentions WHERE case_id = %s "
        "ORDER BY surface_text",
        ev.case_id,
    )
    assert rows == [
        ("(360) 555-0142", chunk_ids[0]),
        ("ABC 123", chunk_ids[2]),
        ("SYN 4821", chunk_ids[0]),
    ]


def test_fake_without_responder_is_treated_as_not_configured(
    owner_conn: Conn, case: Case, worker_settings: WorkerSettings, tmp_path: Path
) -> None:
    ev, _ = case
    router = ModelRouter(RouterSettings(llm_provider="fake"))
    summary = _run(
        owner_conn, ev.evidence_id, make_extract_stage(router), worker_settings, tmp_path
    )
    assert summary["llm"].startswith("skipped: ")


def test_other_model_errors_fail_the_stage(
    owner_conn: Conn, case: Case, worker_settings: WorkerSettings, tmp_path: Path
) -> None:
    ev, _ = case
    router = ModelRouter(RouterSettings(llm_provider="fake"), fake=lambda *_: "not json")
    with pytest.raises(ModelError, match="invalid JSON"):
        _run(owner_conn, ev.evidence_id, make_extract_stage(router), worker_settings, tmp_path)


def test_live_audio_event_uses_chunk_time(
    owner_conn: Conn, case: Case, worker_settings: WorkerSettings, tmp_path: Path
) -> None:
    ev, _ = case
    a = _add_evidence(owner_conn, ev, "2026-03-14T22:48:40-07:00", "audio")
    t0 = datetime.fromisoformat("2026-03-14T22:49:05-07:00")
    chunk = _add_chunks(
        owner_conn, ev.case_id, a, ["SYNTHETIC 911 call: a car is leaving right now!"], [t0]
    )
    fake = FakeModel(
        {
            "entities": [],
            "events": [
                {
                    "description": "Caller reports a car leaving the lot.",
                    "happening_now": True,
                    "chunk_refs": [0],
                    "confidence": 0.7,
                }
            ],
        }
    )
    _run(owner_conn, a, make_extract_stage(fake.router()), worker_settings, tmp_path)
    assert "chunk 0: 2026-03-14T22:49:05-07:00" in fake.calls[0][2]
    rows = _rows(
        owner_conn,
        "SELECT t_start, source_chunk_ids FROM public.events WHERE case_id = %s",
        ev.case_id,
    )
    assert rows == [(t0, chunk)]
