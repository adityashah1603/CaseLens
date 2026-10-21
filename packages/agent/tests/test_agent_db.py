"""DB tests: get_entity / get_timeline / flag_conflict SQL and the audited `ask()` run, as the
caselens_app role under RLS. All rows are SYNTHETIC and rolled back."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from psycopg.types.json import Jsonb

from caselens_agent.loop import AgentConfig
from caselens_agent.runner import CaseAccessError, ask
from caselens_agent.testing import FakeBackends, ScriptedModel, evidence_result, tool_call
from caselens_agent.tools import (
    Conn,
    ToolContext,
    get_entity,
    get_timeline,
    output_errors,
)
from caselens_workers.testing import SeededEvidence, as_role

pytestmark = pytest.mark.db

STATEMENT = "SYNTHETIC. Dale Mercer said he drove the gray sedan SYN4821 into Riverside Lot."


@dataclass
class Graph:
    seeded: SeededEvidence
    chunk_id: uuid.UUID
    mercer: uuid.UUID
    dale: uuid.UUID
    sedan: uuid.UUID
    lot: uuid.UUID
    event_mention: uuid.UUID
    event_location: uuid.UUID
    event_other: uuid.UUID


@pytest.fixture
def graph(owner_conn: Conn, seed_evidence: Callable[..., SeededEvidence], tmp_path: Path) -> Graph:
    path = tmp_path / "statement.pdf"
    path.write_bytes(b"%PDF-1.4 SYNTHETIC test file\n")
    seeded = seed_evidence(path, "pdf")
    conn = owner_conn
    case_id = seeded.case_id
    conn.execute(
        "INSERT INTO public.case_members (case_id, user_id, role) VALUES (%s, %s, 'lead')",
        (case_id, seeded.user_id),
    )
    chunk_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO public.chunks (id, case_id, evidence_id, modality, chunk_index, text, "
        "locator) VALUES (%s, %s, %s, 'pdf', 0, %s, %s)",
        (
            chunk_id,
            case_id,
            seeded.evidence_id,
            STATEMENT,
            Jsonb(
                {
                    "kind": "pdf",
                    "page": 2,
                    "bbox": [72.0, 410.5, 540.0, 452.0],
                    "ocr_confidence": None,
                }
            ),
        ),
    )
    ids = {k: uuid.uuid4() for k in ("mercer", "dale", "sedan", "lot")}
    for key, etype, name, norm, attrs in (
        ("mercer", "person", "Dale Mercer", "dale mercer", {"aliases": ["D. Mercer"]}),
        ("dale", "person", "Dale", "dale", {}),
        ("sedan", "vehicle", "SYN4821", "SYN4821", {"color": "gray", "aliases": ["gray sedan"]}),
        ("lot", "location", "Riverside Lot", "riverside lot", {}),
    ):
        conn.execute(
            "INSERT INTO public.entities (id, case_id, type, canonical_name, normalized_key, "
            "attributes) VALUES (%s, %s, %s, %s, %s, %s)",
            (ids[key], case_id, etype, name, norm, Jsonb(attrs)),
        )
    conn.execute(
        "INSERT INTO public.entity_links (case_id, from_entity_id, to_entity_id, method, "
        "confidence) VALUES (%s, %s, %s, 'fuzzy', 0.9)",
        (case_id, ids["dale"], ids["mercer"]),
    )
    for key, surface, conf in (
        ("mercer", "Dale Mercer", 0.95),
        ("sedan", "SYN4821", 0.9),
        ("lot", "Riverside Lot", 0.9),
    ):
        conn.execute(
            "INSERT INTO public.mentions (case_id, entity_id, chunk_id, surface_text, "
            "confidence) VALUES (%s, %s, %s, %s, %s)",
            (case_id, ids[key], chunk_id, surface, conf),
        )
    events = {k: uuid.uuid4() for k in ("mention", "location", "other")}
    for key, t, loc, chunks, desc in (
        ("mention", "2026-03-14T22:41:00-07:00", None, [chunk_id], "Mercer drives into the lot."),
        ("location", "2026-03-14T22:47:00-07:00", ids["lot"], [], "Sedan leaves the lot."),
        ("other", "2026-03-10T09:00:00-07:00", None, [], "Unrelated SYNTHETIC event."),
    ):
        conn.execute(
            "INSERT INTO public.events (id, case_id, t_start, location_id, description, "
            "source_chunk_ids, confidence) VALUES (%s, %s, %s, %s, %s, %s, 0.8)",
            (events[key], case_id, t, loc, desc, chunks),
        )
    as_role(conn, "caselens_app", seeded.user_id)
    return Graph(
        seeded,
        chunk_id,
        ids["mercer"],
        ids["dale"],
        ids["sedan"],
        ids["lot"],
        events["mention"],
        events["location"],
        events["other"],
    )


def ctx_for(conn: Conn, g: Graph) -> ToolContext:
    return ToolContext(conn, conn, g.seeded.case_id)


def test_get_entity_by_name_alias_plate_and_id(owner_conn: Conn, graph: Graph) -> None:
    ctx = ctx_for(owner_conn, graph)
    out = get_entity(ctx, {"name": "mercer", "type": "person"})
    assert output_errors("get_entity", out) == []
    top = out["matches"][0]
    assert top["entity_id"] == str(graph.mercer)
    assert top["aliases"] == ["D. Mercer"]
    assert top["linked_entity_ids"] == [str(graph.dale)]
    mention = top["mentions"][0]
    assert mention["chunk_id"] == str(graph.chunk_id)
    assert mention["evidence_id"] == str(graph.seeded.evidence_id)
    assert mention["locator"]["page"] == 2

    assert get_entity(ctx, {"name": "syn-4821"})["matches"][0]["entity_id"] == str(graph.sedan)
    assert get_entity(ctx, {"name": "Gray Sedan"})["matches"][0]["entity_id"] == str(graph.sedan)
    by_id = get_entity(ctx, {"entity_id": str(graph.dale)})
    assert by_id["matches"][0]["linked_entity_ids"] == [str(graph.mercer)]
    assert get_entity(ctx, {"name": "100%_"})["matches"] == []


def test_get_timeline_by_entity_links_location_and_window(owner_conn: Conn, graph: Graph) -> None:
    ctx = ctx_for(owner_conn, graph)
    # Dale is linked to Mercer, whose mention is in the event's source chunk.
    out = get_timeline(ctx, {"entity_id": str(graph.dale)})
    assert output_errors("get_timeline", out) == []
    assert [e["event_id"] for e in out["events"]] == [str(graph.event_mention)]
    src = out["events"][0]["sources"][0]
    assert src == {
        "chunk_id": str(graph.chunk_id),
        "evidence_id": str(graph.seeded.evidence_id),
        "locator": {
            "kind": "pdf",
            "page": 2,
            "bbox": [72.0, 410.5, 540.0, 452.0],
            "ocr_confidence": None,
        },
    }
    by_loc = get_timeline(ctx, {"entity_id": str(graph.lot)})
    assert [e["event_id"] for e in by_loc["events"]] == [
        str(graph.event_mention),
        str(graph.event_location),
    ]
    assert by_loc["events"][1]["location"] == {"entity_id": str(graph.lot), "name": "Riverside Lot"}
    window = get_timeline(
        ctx, {"time_from": "2026-03-14T00:00:00-07:00", "time_to": "2026-03-15T00:00:00-07:00"}
    )
    assert str(graph.event_other) not in [e["event_id"] for e in window["events"]]
    assert len(window["events"]) == 2


def _scripted(chunk: dict[str, Any]) -> tuple[ScriptedModel, FakeBackends]:
    fakes = FakeBackends(evidence=[chunk])
    model = ScriptedModel(
        plan=[
            tool_call("get_entity", {"name": "Dale Mercer", "type": "person"}),
            tool_call("search_evidence", {"query": "who drove the gray sedan"}),
            tool_call(
                "flag_conflict",
                {
                    "description": "Driver identity (SYNTHETIC test)",
                    "claim_a": {"text": "Mercer drove.", "source_refs": ["S1"]},
                    "claim_b": {"text": "Someone else drove.", "source_refs": ["S1"]},
                },
            ),
        ],
        synthesize=[
            {
                "status": "answered",
                "answer": "SYNTHETIC. Dale Mercer drove the gray sedan.",
                "claims": [
                    {
                        "text": "Dale Mercer said he drove the gray sedan.",
                        "citations": [{"source": "S1", "quote": "he drove the gray sedan"}],
                    }
                ],
                "conflicts": [
                    {
                        "description": "Driver identity",
                        "claim_a": {"text": "Mercer drove.", "citations": [{"source": "S1"}]},
                        "claim_b": {"text": "Someone else drove.", "citations": [{"source": "S1"}]},
                        "flag": "F1",
                    }
                ],
                "confidence": 0.8,
                "open_questions": [],
            }
        ],
    )
    return model, fakes


def test_ask_writes_run_tool_calls_conflict_and_audit_as_app(
    owner_conn: Conn, graph: Graph
) -> None:
    conn = owner_conn
    chunk = evidence_result(
        STATEMENT, page=2, chunk_id=graph.chunk_id, evidence_id=graph.seeded.evidence_id
    )
    chunk["locator"] = {
        "kind": "pdf",
        "page": 2,
        "bbox": [72.0, 410.5, 540.0, 452.0],
        "ocr_confidence": None,
    }
    model, fakes = _scripted(chunk)
    case_id, user_id = graph.seeded.case_id, graph.seeded.user_id
    assert conn.execute("SELECT current_user").fetchone() == ("caselens_app",)

    result = ask(
        conn,
        conn,
        case_id,
        user_id,
        "Who drove the gray sedan?",
        router=model.router(),
        backends=fakes.backends(),
        config=AgentConfig(prefetch=False),  # the script covers every tool call
    )

    assert result.status == "succeeded" and not result.fell_back
    assert result.answer["status"] == "answered"
    assert [t.ok for t in result.tool_calls] == [True, True, True]
    row = conn.execute(
        "SELECT status, user_id, jsonb_array_length(tool_calls), answer -> 'status', "
        "jsonb_array_length(citations), model, latency_ms, plan ? 'thoughts' "
        "FROM public.agent_runs WHERE id = %s",
        (result.run_id,),
    ).fetchone()
    assert row is not None
    # one citation with the quote, one without (claims vs conflict sides)
    assert row[:5] == ("succeeded", user_id, 3, "answered", 2)
    assert row[5] == "fake:fake" and row[6] is not None and row[7] is True
    usage = conn.execute(
        "SELECT input_tokens, output_tokens, cost_usd, plan -> 'usage' -> 'model_calls' "
        "FROM public.agent_runs WHERE id = %s",
        (result.run_id,),
    ).fetchone()
    # the fake model reports no tokens; 3 planning turns + finish + 1 synthesis = 5 calls
    assert usage is not None and usage[:3] == (0, 0, 0) and usage[3] == 5

    conflict = conn.execute(
        "SELECT id::text, agent_run_id, status, claim_a -> 'source_refs' -> 0 ->> 'chunk_id' "
        "FROM public.conflicts WHERE case_id = %s",
        (case_id,),
    ).fetchall()
    assert len(conflict) == 1
    assert conflict[0][1:] == (result.run_id, "open", str(graph.chunk_id))
    assert result.answer["conflicts"][0]["conflict_record_id"] == conflict[0][0]

    audit = conn.execute(
        "SELECT action, user_id, case_id, payload FROM public.audit_log "
        "WHERE payload ->> 'run_id' = %s ORDER BY id",
        (str(result.run_id),),
    ).fetchall()
    assert [a[0] for a in audit] == ["agent.ask"] + ["agent.tool_call"] * 3
    assert all(a[1] == user_id and a[2] == case_id for a in audit)
    tools = [a[3]["tool"] for a in audit[1:]]
    assert tools == ["get_entity", "search_evidence", "flag_conflict"]
    # payloads carry counts, never the evidence text itself
    assert all(STATEMENT not in str(a[3]) for a in audit)
    assert audit[2][3]["result_count"] == 1 and audit[2][3]["source_count"] == 1

    # a finished run is frozen: the app role can no longer update it
    updated = conn.execute(
        "UPDATE public.agent_runs SET latency_ms = 1 WHERE id = %s", (result.run_id,)
    ).rowcount
    assert updated == 0


def test_non_member_cannot_run_on_the_case(owner_conn: Conn, graph: Graph) -> None:
    conn = owner_conn
    outsider = uuid.uuid4()
    as_role(conn, None)
    conn.execute(
        "INSERT INTO public.users (id, oidc_subject) VALUES (%s, %s)",
        (outsider, f"test:{outsider}"),
    )
    as_role(conn, "caselens_app", outsider)
    model, fakes = _scripted(evidence_result(STATEMENT))
    with pytest.raises(CaseAccessError):
        ask(
            conn,
            conn,
            graph.seeded.case_id,
            outsider,
            "Who drove the gray sedan?",
            router=model.router(),
            backends=fakes.backends(),
        )
    assert model.calls == [] and fakes.calls == []
    # and the case's graph is invisible to the outsider
    assert get_entity(ctx_for(conn, graph), {"name": "Dale Mercer"})["matches"] == []
    as_role(conn, None)
    runs = conn.execute(
        "SELECT count(*) FROM public.agent_runs WHERE case_id = %s", (graph.seeded.case_id,)
    ).fetchone()
    assert runs == (0,)


def test_demo_user_conflicts_are_answered_but_never_stored(owner_conn: Conn, graph: Graph) -> None:
    """openapi /ask: for the shared demo user (users.is_demo) flag_conflict writes nothing and
    the answer's conflict_record_id is null; the conflict is still shown in the answer."""
    conn = owner_conn
    case_id, user_id = graph.seeded.case_id, graph.seeded.user_id
    as_role(conn, None)
    conn.execute("UPDATE public.users SET is_demo = true WHERE id = %s", (user_id,))
    as_role(conn, "caselens_app", user_id)
    chunk = evidence_result(
        STATEMENT, page=2, chunk_id=graph.chunk_id, evidence_id=graph.seeded.evidence_id
    )
    chunk["locator"] = {
        "kind": "pdf",
        "page": 2,
        "bbox": [72.0, 410.5, 540.0, 452.0],
        "ocr_confidence": None,
    }
    model, fakes = _scripted(chunk)
    result = ask(
        conn,
        conn,
        case_id,
        user_id,
        "Who drove the gray sedan?",
        router=model.router(),
        backends=fakes.backends(),
        config=AgentConfig(prefetch=False),
    )
    assert result.status == "succeeded" and not result.fell_back
    assert [t.tool for t in result.tool_calls][-1] == "flag_conflict"
    assert result.tool_calls[-1].ok and result.tool_calls[-1].conflict_id is None
    assert result.answer["status"] == "answered"
    shown = result.answer["conflicts"]
    assert len(shown) == 1 and shown[0]["conflict_record_id"] is None
    as_role(conn, None)
    stored = conn.execute(
        "SELECT count(*) FROM public.conflicts WHERE case_id = %s", (case_id,)
    ).fetchone()
    assert stored == (0,)
    audit = conn.execute(
        "SELECT payload FROM public.audit_log WHERE payload ->> 'run_id' = %s "
        "AND action = 'agent.tool_call' AND payload ->> 'tool' = 'flag_conflict'",
        (str(result.run_id),),
    ).fetchone()
    assert audit is not None and audit[0]["ok"] is True and audit[0]["conflict_id"] is None
