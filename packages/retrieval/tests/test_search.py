"""Hybrid search on a tiny SYNTHETIC case, with a fake embedder and reranker (no models)."""

from __future__ import annotations

import hashlib
import math
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from caselens_agent.contracts import load_tool_definitions
from caselens_retrieval import (
    EvidenceFilters,
    PdfLocator,
    SearchEvidenceInput,
    SearchVideoInput,
    rrf_fuse,
    search_evidence,
    search_video,
)
from caselens_retrieval.models import LOCATOR_ADAPTER
from caselens_retrieval.search import fts_or_query, rerank_snippet
from caselens_workers.db import Conn
from caselens_workers.testing import as_role

T0 = datetime(2026, 3, 14, 22, 0, tzinfo=UTC)
WORD = re.compile(r"\w+")


def fake_embed(text: str) -> list[float]:
    """Hashed bag of words: texts sharing words are close in cosine distance."""
    vec = [0.0] * 768
    vec[0] = 0.01
    for w in WORD.findall(text.lower()):
        vec[int(hashlib.md5(w.encode()).hexdigest(), 16) % 767 + 1] += 1.0  # noqa: S324
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec]


def fake_rerank(query: str, docs: Sequence[str]) -> list[float]:
    q = set(WORD.findall(query.lower()))
    return [float(len(q & set(WORD.findall(d.lower())))) for d in docs]


def validator(tool: str) -> Draft202012Validator:
    schema = load_tool_definitions()[tool]["output_schema"]
    return Draft202012Validator(schema, format_checker=FormatChecker())


# --- pure functions --------------------------------------------------------------------------


def test_rrf_sums_reciprocal_ranks_and_breaks_ties_deterministically() -> None:
    a, b, c, d = (uuid.UUID(int=i) for i in range(1, 5))
    fused = rrf_fuse([[a, b, c], [c, b, d]], k=60)
    assert [x for x, _ in fused] == [c, b, a, d]
    assert fused[0][1] == pytest.approx(1 / 63 + 1 / 61)
    assert fused[1][1] == pytest.approx(2 / 62)
    # Equal scores and equal best ranks fall back to id order.
    assert [x for x, _ in rrf_fuse([[b], [a]])] == [a, b]


def test_fts_query_ors_terms_and_drops_websearch_syntax() -> None:
    assert fts_or_query('Who was in the "gray" sedan -or (March 14)?') == (
        "Who or was or in or the or gray or sedan or March or 14"
    )
    assert fts_or_query("  ") == ""


def test_rerank_snippet_picks_the_window_with_most_query_terms() -> None:
    text = " ".join(["filler"] * 100 + ["gray", "sedan", "headlights", "off"] + ["filler"] * 100)
    snippet = rerank_snippet("gray sedan with headlights off", text, 20)
    assert "gray sedan headlights off" in snippet
    assert len(snippet.split()) == 20
    assert rerank_snippet("x", "short text", 20) == "short text"


def test_locator_round_trips_verbatim() -> None:
    stored = {"kind": "pdf", "page": 2, "bbox": [1.0, 2.0, 3.0, 4.0]}
    assert LOCATOR_ADAPTER.validate_python(stored).model_dump(mode="json") == stored
    assert PdfLocator(page=1).model_dump(mode="json") == {"kind": "pdf", "page": 1}
    with pytest.raises(ValidationError):
        LOCATOR_ADAPTER.validate_python({"kind": "pdf", "page": 1, "extra": 1})


def test_inputs_follow_the_contract() -> None:
    SearchEvidenceInput.model_validate(
        {"query": "q", "filters": {"modalities": ["pdf"], "time_from": "2026-03-14T22:00:00Z"}}
    )
    SearchVideoInput.model_validate({"query": "q", "top_k": 20})
    bad: list[dict[str, Any]] = [
        {"query": ""},
        {"query": "q", "top_k": 21},
        {"query": "q", "filters": {"modalities": []}},
        {"query": "q", "filters": {"modalities": ["pdf", "pdf"]}},
        {"query": "q", "filters": {"time_from": "2026-03-14T22:00:00"}},  # no UTC offset
        {"query": "q", "filters": {"case_id": str(uuid.uuid4())}},
    ]
    for payload in bad:
        with pytest.raises(ValidationError):
            SearchEvidenceInput.model_validate(payload)


# --- database --------------------------------------------------------------------------------


@dataclass
class Case:
    case_id: uuid.UUID
    lead: uuid.UUID
    outsider: uuid.UUID
    ev: dict[str, uuid.UUID]
    chunks: dict[str, uuid.UUID]
    entities: dict[str, uuid.UUID]


def _vec(text: str) -> str:
    return "[" + ",".join(repr(x) for x in fake_embed(text)) + "]"


@pytest.fixture
def case(owner_conn: Conn) -> Case:
    as_role(owner_conn, None)
    lead, outsider, case_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    for u in (lead, outsider):
        owner_conn.execute(
            "INSERT INTO public.users (id, oidc_subject) VALUES (%s, %s)", (u, f"test:{u}")
        )
    owner_conn.execute(
        "INSERT INTO public.cases (id, name, created_by) VALUES (%s, 'SYNTHETIC search', %s)",
        (case_id, lead),
    )
    owner_conn.execute(
        "INSERT INTO public.case_members (case_id, user_id, role) VALUES (%s, %s, 'lead')",
        (case_id, lead),
    )
    ev: dict[str, uuid.UUID] = {}
    for name, typ in (("pdf", "pdf"), ("audio", "audio"), ("video", "video"), ("csv", "csv")):
        ev[name] = uuid.uuid4()
        owner_conn.execute(
            "INSERT INTO public.evidence (id, case_id, type, original_filename, content_type, "
            "size_bytes, gcs_uri, sha256, uploaded_by) "
            "VALUES (%s, %s, %s, %s, 'application/octet-stream', 1, %s, %s, %s)",
            (
                ev[name],
                case_id,
                typ,
                f"synthetic_{name}",
                f"memory://{name}",
                hashlib.sha256(name.encode()).hexdigest(),
                lead,
            ),
        )
    rows: list[tuple[str, str, str, dict[str, Any], datetime | None, datetime | None]] = [
        (
            "pdf_sedan",
            "pdf",
            "SYNTHETIC The gray sedan left the lot with its headlights off.",
            {"kind": "pdf", "page": 1, "bbox": [10.0, 10.0, 200.0, 40.0]},
            None,
            None,
        ),
        (
            "pdf_casing",
            "pdf",
            "SYNTHETIC A brass cartridge casing was found near the pickup.",
            {"kind": "pdf", "page": 2, "bbox": None, "ocr_confidence": 0.9},
            None,
            None,
        ),
        (
            "audio_911",
            "audio",
            "SYNTHETIC caller: a man has been shot at the river lot, a gray car just left.",
            {"kind": "audio", "start_ms": 1000, "end_ms": 5000, "speaker": "SPEAKER_1"},
            T0 + timedelta(minutes=48),
            T0 + timedelta(minutes=48, seconds=4),
        ),
        (
            "video_track",
            "video",
            "SYNTHETIC Track 2 (car, gray) enters the lot and parks.",
            {
                "kind": "video",
                "start_ms": 2000,
                "end_ms": 14000,
                "frame_idx": 210,
                "bbox": [0.0, 1.0, 2.0, 3.0],
                "track_id": "t2",
            },
            T0 + timedelta(minutes=41),
            T0 + timedelta(minutes=41, seconds=12),
        ),
        (
            "video_scene",
            "video",
            "SYNTHETIC Scene 1: an empty parking lot at night.",
            {"kind": "video", "start_ms": 0, "end_ms": 30000},
            T0 + timedelta(minutes=10),
            T0 + timedelta(minutes=10, seconds=30),
        ),
        (
            "csv_row",
            "csv",
            "SYNTHETIC Plate read row 5: plate SYN0001 read at the lot entrance.",
            {"kind": "record", "table": "plate_reads", "row_id": 5},
            T0 + timedelta(minutes=41),
            None,
        ),
    ]
    chunks: dict[str, uuid.UUID] = {}
    index: dict[str, int] = {}
    for key, modality, text, locator, t_start, t_end in rows:
        chunks[key] = uuid.uuid4()
        index[modality] = index.get(modality, -1) + 1
        owner_conn.execute(
            "INSERT INTO public.chunks (id, case_id, evidence_id, modality, chunk_index, text, "
            "embedding, t_start, t_end, locator) VALUES (%s, %s, %s, %s, %s, %s, %s::vector, "
            "%s, %s, %s)",
            (
                chunks[key],
                case_id,
                ev[modality],
                modality,
                index[modality],
                text,
                _vec(text),
                t_start,
                t_end,
                Jsonb(locator),
            ),
        )
    entities: dict[str, uuid.UUID] = {}
    for key, typ, name in (
        ("sedan", "vehicle", "gray sedan"),
        ("sedan_alias", "vehicle", "gray car"),
        ("pickup", "vehicle", "pickup"),
        ("pickup_alias", "vehicle", "white truck"),
    ):
        entities[key] = uuid.uuid4()
        owner_conn.execute(
            "INSERT INTO public.entities (id, case_id, type, canonical_name) "
            "VALUES (%s, %s, %s, %s)",
            (entities[key], case_id, typ, name),
        )
    for entity, chunk in (
        ("sedan", "pdf_sedan"),
        ("sedan_alias", "audio_911"),
        ("pickup_alias", "pdf_casing"),
    ):
        owner_conn.execute(
            "INSERT INTO public.mentions (case_id, entity_id, chunk_id, surface_text) "
            "VALUES (%s, %s, %s, 'x')",
            (case_id, entities[entity], chunks[chunk]),
        )
    # alias -> canonical (active), and a reverted link that must be ignored.
    owner_conn.execute(
        "INSERT INTO public.entity_links (case_id, from_entity_id, to_entity_id, method) "
        "VALUES (%s, %s, %s, 'fuzzy')",
        (case_id, entities["sedan_alias"], entities["sedan"]),
    )
    owner_conn.execute(
        "INSERT INTO public.entity_links (case_id, from_entity_id, to_entity_id, method, "
        "status, reverted_at) VALUES (%s, %s, %s, 'fuzzy', 'reverted', now())",
        (case_id, entities["pickup_alias"], entities["pickup"]),
    )
    owner_conn.execute(
        "INSERT INTO public.events (case_id, t_start, description, source_chunk_ids) "
        "VALUES (%s, %s, 'SYNTHETIC sedan leaves', %s)",
        (case_id, T0 + timedelta(minutes=47), [chunks["pdf_sedan"]]),
    )
    as_role(owner_conn, "caselens_app", lead)
    return Case(case_id, lead, outsider, ev, chunks, entities)


def run(
    conn: Conn, case: Case, query: str, filters: EvidenceFilters | None = None, **kw: Any
) -> list[uuid.UUID]:
    out = search_evidence(
        conn, case.case_id, query, filters, embedder=fake_embed, reranker=fake_rerank, **kw
    )
    validator("search_evidence").validate(out.model_dump(mode="json"))
    return [r.chunk_id for r in out.results]


@pytest.mark.db
def test_search_evidence_ranks_and_matches_contract(owner_conn: Conn, case: Case) -> None:
    out = search_evidence(
        owner_conn,
        case.case_id,
        "gray sedan headlights off",
        embedder=fake_embed,
        reranker=fake_rerank,
    )
    data = out.model_dump(mode="json")
    validator("search_evidence").validate(data)
    assert out.results[0].chunk_id == case.chunks["pdf_sedan"]
    assert out.results[0].score == 4.0  # the fake reranker's score, not RRF
    assert data["results"][0]["locator"] == {
        "kind": "pdf",
        "page": 1,
        "bbox": [10.0, 10.0, 200.0, 40.0],
    }
    assert len(out.results) == 6

    rrf = search_evidence(
        owner_conn, case.case_id, "gray sedan headlights off", embedder=fake_embed, rerank=False
    )
    assert rrf.results[0].chunk_id == case.chunks["pdf_sedan"]
    assert 0 < rrf.results[0].score <= 2 / 61
    assert len(run(owner_conn, case, "gray", top_k=2)) == 2


@pytest.mark.db
def test_reranker_reorders_fused_candidates(owner_conn: Conn, case: Case) -> None:
    def prefer_casing(query: str, docs: Sequence[str]) -> list[float]:
        return [10.0 if "casing" in d else 0.0 for d in docs]

    out = search_evidence(
        owner_conn, case.case_id, "lot", embedder=fake_embed, reranker=prefer_casing
    )
    assert out.results[0].chunk_id == case.chunks["pdf_casing"]


@pytest.mark.db
def test_modality_and_evidence_filters(owner_conn: Conn, case: Case) -> None:
    ids = run(owner_conn, case, "gray lot", EvidenceFilters(modalities=["audio", "csv"]))
    assert set(ids) == {case.chunks["audio_911"], case.chunks["csv_row"]}
    ids = run(owner_conn, case, "gray lot", EvidenceFilters(evidence_ids=[case.ev["video"]]))
    assert set(ids) == {case.chunks["video_track"], case.chunks["video_scene"]}


@pytest.mark.db
def test_entity_filter_follows_active_links_both_ways(owner_conn: Conn, case: Case) -> None:
    expected = {case.chunks["pdf_sedan"], case.chunks["audio_911"]}
    for entity in ("sedan", "sedan_alias"):
        f = EvidenceFilters(entity_ids=[case.entities[entity]])
        assert set(run(owner_conn, case, "gray lot", f)) == expected
    # The pickup link is reverted, so the alias's mention is not reached from the canonical.
    f = EvidenceFilters(entity_ids=[case.entities["pickup"]])
    assert run(owner_conn, case, "casing pickup", f) == []


@pytest.mark.db
def test_time_window_overlap_and_untimed_chunks_via_events(owner_conn: Conn, case: Case) -> None:
    f = EvidenceFilters(time_from=T0 + timedelta(minutes=45), time_to=T0 + timedelta(minutes=50))
    # audio overlaps; pdf_sedan is cited by an event at 22:47; pdf_casing has no event.
    assert set(run(owner_conn, case, "gray lot casing", f)) == {
        case.chunks["audio_911"],
        case.chunks["pdf_sedan"],
    }
    f = EvidenceFilters(time_to=T0 + timedelta(minutes=20))
    assert set(run(owner_conn, case, "gray lot casing", f)) == {case.chunks["video_scene"]}
    f = EvidenceFilters(time_from=T0 + timedelta(minutes=41, seconds=5))
    assert case.chunks["video_track"] in run(owner_conn, case, "gray lot", f)


@pytest.mark.db
def test_rls_hides_the_case_from_non_members(owner_conn: Conn, case: Case) -> None:
    as_role(owner_conn, "caselens_app", case.outsider)
    assert run(owner_conn, case, "gray sedan lot") == []
    out = search_video(owner_conn, case.case_id, "gray car", embedder=fake_embed)
    assert out.results == []


@pytest.mark.db
def test_other_cases_are_not_searched(owner_conn: Conn, case: Case) -> None:
    assert run(owner_conn, case, "gray sedan", top_k=1)
    other = Case(uuid.uuid4(), case.lead, case.outsider, {}, {}, {})
    assert run(owner_conn, other, "gray sedan") == []


@pytest.mark.db
def test_search_video_returns_tracks_with_keyframes(owner_conn: Conn, case: Case) -> None:
    out = search_video(
        owner_conn, case.case_id, "gray car enters", embedder=fake_embed, reranker=fake_rerank
    )
    data = out.model_dump(mode="json")
    validator("search_video").validate(data)
    assert {r.chunk_id for r in out.results} == {
        case.chunks["video_track"],
        case.chunks["video_scene"],
    }
    top = data["results"][0]
    assert top["chunk_id"] == str(case.chunks["video_track"])
    assert top["track_id"] == "t2"
    assert top["start_ms"] == 2000
    assert top["end_ms"] == 14000
    assert top["keyframes"] == [{"frame_idx": 210, "bbox": [0.0, 1.0, 2.0, 3.0], "image_uri": None}]
    assert datetime.fromisoformat(top["wall_clock_start"]) == T0 + timedelta(minutes=41)
    scene = data["results"][1]
    assert scene["keyframes"] == []
    assert scene["track_id"] is None
    assert scene["locator"] == {"kind": "video", "start_ms": 0, "end_ms": 30000}

    windowed = search_video(
        owner_conn,
        case.case_id,
        "gray car enters",
        time_from=T0,
        time_to=T0 + timedelta(minutes=15),
        embedder=fake_embed,
        reranker=fake_rerank,
    )
    assert [r.chunk_id for r in windowed.results] == [case.chunks["video_scene"]]


@pytest.mark.db
def test_bad_arguments_are_rejected(owner_conn: Conn, case: Case) -> None:
    with pytest.raises(ValueError, match="top_k"):
        run(owner_conn, case, "gray", top_k=0)
    with pytest.raises(ValueError, match="query"):
        run(owner_conn, case, "   ")
