"""Contract tests for every /v1 endpoint against a real Postgres (RLS on), with dev tokens and an
in-memory evidence store. Every response body is validated against docs/contracts/openapi.yaml.
All data is SYNTHETIC."""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import make_conninfo

from caselens_agent.loop import AgentConfig
from caselens_agent.testing import FakeBackends, ScriptedModel, evidence_result, tool_call
from caselens_api.auth import CompositeVerifier
from caselens_api.db import Database
from caselens_api.deps import RateLimiter, Services
from caselens_api.main import create_app
from caselens_api.settings import Settings
from caselens_api.testing import STATEMENT, World, check, owner_rows, schema_errors
from caselens_workers.settings import libpq_url

pytestmark = pytest.mark.db


def services(client: TestClient) -> Services:
    svc: Services = client.app.state.services  # type: ignore[attr-defined]
    return svc


def audit_actions(world: World, case_id: uuid.UUID | None, user: str) -> list[str]:
    rows = owner_rows(
        world.url,
        "SELECT action FROM public.audit_log WHERE case_id IS NOT DISTINCT FROM %s "
        "AND user_id = %s ORDER BY id",
        (case_id, world.users[user]),
    )
    return [r[0] for r in rows]


def new_case(client: TestClient, world: World, name: str = "SYNTHETIC scratch case") -> str:
    body = check(
        client.post("/v1/cases", json={"name": name}, headers=world.auth("lead")), 201, "Case"
    )
    return str(body["id"])


# ---- connections and auth ------------------------------------------------------------------


def test_request_connections_run_as_app_role_and_reset(world: World) -> None:
    db = Database(world.url, "caselens_app", "caselens_readonly", 1, 1)
    db.pool.open(wait=True)
    try:
        uid = world.users["lead"]
        with db.session(uid) as conn:
            row = conn.execute("SELECT current_user, current_setting('app.user_id')").fetchone()
            assert row == ("caselens_app", str(uid))
        with db.session(uid, readonly=True) as conn:
            row = conn.execute("SELECT current_user").fetchone()
            assert row == ("caselens_readonly",)
        with db.pool.connection() as raw:  # back in the pool: identity and role are reset
            row = raw.execute(
                "SELECT current_user = session_user, current_setting('app.user_id', true)"
            ).fetchone()
            assert row == (True, "")
    finally:
        db.close()


def test_missing_or_bad_tokens_get_401_problem(client: TestClient, world: World) -> None:
    check(client.get("/v1/me"), 401)
    check(client.get("/v1/me", headers={"Authorization": "Bearer not-a-jwt"}), 401)
    check(client.get("/v1/me", headers={"Authorization": "Basic abc"}), 401)
    forged = world.tokens["lead"][:-4] + "AAAA"
    check(client.get("/v1/me", headers={"Authorization": f"Bearer {forged}"}), 401)
    check(client.get(f"/v1/cases/{world.case_id}/timeline"), 401)


def test_dev_verifier_refused_outside_local() -> None:
    from pydantic import SecretStr

    from caselens_api.auth import build_verifier

    with pytest.raises(RuntimeError, match="only allowed locally"):
        build_verifier(Settings(environment="prod", dev_auth_secret=SecretStr("x" * 40)))


def test_me_and_first_sign_in(client: TestClient, world: World) -> None:
    me = check(client.get("/v1/me", headers=world.auth("lead")), 200, "Me")
    assert me["user"]["id"] == str(world.users["lead"])
    assert {m["case_id"]: m["role"] for m in me["memberships"]} == {str(world.case_id): "lead"}
    first = check(client.get("/v1/me", headers=world.auth("newcomer")), 200, "Me")
    assert first["memberships"] == [] and first["user"]["email"].startswith("new.")
    again = check(client.get("/v1/me", headers=world.auth("newcomer")), 200, "Me")
    assert again["user"]["id"] == first["user"]["id"]


# ---- cases ---------------------------------------------------------------------------------


def test_case_detail_and_non_member_404(client: TestClient, world: World) -> None:
    case = check(
        client.get(f"/v1/cases/{world.case_id}", headers=world.auth("viewer")), 200, "Case"
    )
    assert case["my_role"] == "viewer" and case["evidence_count"] >= 1
    assert case["ingest_summary"]["succeeded"] >= 2 and case["suggested_questions"] == []
    assert case["last_activity_at"] is not None
    check(client.get(f"/v1/cases/{world.case_id}", headers=world.auth("outsider")), 404)
    check(client.get(f"/v1/cases/{uuid.uuid4()}", headers=world.auth("lead")), 404)
    check(client.get("/v1/cases/not-a-uuid", headers=world.auth("lead")), 404)


def test_create_list_paginate_cases(client: TestClient, world: World) -> None:
    check(client.post("/v1/cases", json={"name": "x"}, headers=world.auth("demo")), 403)
    check(client.post("/v1/cases", json={"name": ""}, headers=world.auth("lead")), 422)
    check(client.post("/v1/cases", json={"name": "a", "x": 1}, headers=world.auth("lead")), 422)
    created = {new_case(client, world, f"SYNTHETIC page {i}") for i in range(2)}
    assert audit_actions(world, uuid.UUID(next(iter(created))), "lead") == ["case.create"]
    seen: list[str] = []
    cursor = None
    for _ in range(10):
        params: dict[str, Any] = {"limit": 1} | ({"cursor": cursor} if cursor else {})
        page = check(
            client.get("/v1/cases", params=params, headers=world.auth("lead")), 200, "CaseList"
        )
        seen += [c["id"] for c in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) and created | {str(world.case_id)} <= set(seen)
    outsider = check(client.get("/v1/cases", headers=world.auth("outsider")), 200, "CaseList")
    assert [c["id"] for c in outsider["items"]] == [str(world.other_case_id)]
    check(client.get("/v1/cases", params={"cursor": "garbage!"}, headers=world.auth("lead")), 422)
    check(client.get("/v1/cases", params={"limit": 101}, headers=world.auth("lead")), 422)


def test_update_case_role_matrix(client: TestClient, world: World) -> None:
    url = f"/v1/cases/{world.case_id}"
    body = {"description": "SYNTHETIC updated"}
    check(client.patch(url, json=body), 401)
    check(client.patch(url, json=body, headers=world.auth("outsider")), 404)
    check(client.patch(url, json=body, headers=world.auth("viewer")), 403)
    check(client.patch(url, json=body, headers=world.auth("analyst")), 403)
    check(client.patch(url, json={}, headers=world.auth("lead")), 422)
    updated = check(client.patch(url, json=body, headers=world.auth("lead")), 200, "Case")
    assert updated["description"] == "SYNTHETIC updated"
    assert "case.update" in audit_actions(world, world.case_id, "lead")


# ---- members -------------------------------------------------------------------------------


def test_member_management(client: TestClient, world: World) -> None:
    case_id = new_case(client, world)
    base = f"/v1/cases/{case_id}/members"
    lead = world.auth("lead")
    target = str(world.users["analyst"])
    check(
        client.post(
            base, json={"user_id": target, "role": "viewer"}, headers=world.auth("outsider")
        ),
        404,
    )
    added = check(
        client.post(base, json={"user_id": target, "role": "viewer"}, headers=lead),
        201,
        "CaseMember",
    )
    assert added["role"] == "viewer" and added["added_by"] == str(world.users["lead"])
    check(client.post(base, json={"user_id": target, "role": "viewer"}, headers=lead), 409)
    check(
        client.post(base, json={"user_id": str(uuid.uuid4()), "role": "viewer"}, headers=lead), 404
    )
    check(client.post(base, json={"role": "viewer"}, headers=lead), 422)
    email = owner_rows(
        world.url, "SELECT email FROM public.users WHERE id = %s", (world.users["viewer"],)
    )[0][0]
    check(
        client.post(base, json={"email": email, "role": "viewer"}, headers=lead), 201, "CaseMember"
    )
    # viewers (and analysts) cannot manage members
    check(
        client.patch(f"{base}/{target}", json={"role": "lead"}, headers=world.auth("analyst")), 403
    )
    changed = check(
        client.patch(f"{base}/{target}", json={"role": "analyst"}, headers=lead), 200, "CaseMember"
    )
    assert changed["role"] == "analyst"
    lead_id = str(world.users["lead"])
    check(client.patch(f"{base}/{lead_id}", json={"role": "viewer"}, headers=lead), 409)
    check(client.delete(f"{base}/{lead_id}", headers=lead), 409)
    check(client.delete(f"{base}/{target}", headers=world.auth("analyst")), 403)
    assert client.delete(f"{base}/{target}", headers=lead).status_code == 204
    check(client.delete(f"{base}/{target}", headers=lead), 404)
    page = check(client.get(base, params={"limit": 1}, headers=lead), 200, "CaseMemberList")
    assert page["next_cursor"] is not None
    rest = check(
        client.get(base, params={"cursor": page["next_cursor"]}, headers=lead),
        200,
        "CaseMemberList",
    )
    assert {m["user_id"] for m in page["items"] + rest["items"]} == {
        lead_id,
        str(world.users["viewer"]),
    }
    actions = audit_actions(world, uuid.UUID(case_id), "lead")
    assert actions == ["case.create", "member.add", "member.add", "member.update", "member.remove"]


# ---- evidence ------------------------------------------------------------------------------


def test_evidence_list_get_jobs(client: TestClient, world: World) -> None:
    base = f"/v1/cases/{world.case_id}/evidence"
    page = check(client.get(base, headers=world.auth("viewer")), 200, "EvidenceList")
    ev = next(e for e in page["items"] if e["id"] == str(world.evidence_id))
    assert [j["stage"] for j in ev["ingest_jobs"]] == ["parse", "chunk"]
    assert "gcs_uri" not in ev
    empty = check(
        client.get(base, params={"type": "video"}, headers=world.auth("viewer")),
        200,
        "EvidenceList",
    )
    assert empty["items"] == []
    check(client.get(base, params={"type": "docx"}, headers=world.auth("viewer")), 422)
    one = check(
        client.get(f"{base}/{world.evidence_id}", headers=world.auth("viewer")), 200, "Evidence"
    )
    assert one["chain_of_custody"][0]["action"] == "uploaded"
    jobs = check(
        client.get(f"{base}/{world.evidence_id}/jobs", headers=world.auth("viewer")),
        200,
        "IngestJobList",
    )
    assert len(jobs["items"]) == 2 and jobs["next_cursor"] is None
    check(client.get(f"{base}/{world.evidence_id}", headers=world.auth("outsider")), 404)
    check(client.get(f"{base}/{uuid.uuid4()}", headers=world.auth("viewer")), 404)


def test_upload_roles_duplicate_and_content(client: TestClient, world: World) -> None:
    base = f"/v1/cases/{world.case_id}/evidence"
    data = b"SYNTHETIC,csv\n" + uuid.uuid4().hex.encode() + b",1\n"
    files = {"file": ("SYNTHETIC_calls.csv", data, "text/csv")}
    form = {"description": "SYNTHETIC test upload", "recorded_at": "2026-03-14T22:00:00Z"}
    check(client.post(base, files=files, data=form, headers=world.auth("viewer")), 403)
    check(client.post(base, files=files, data=form, headers=world.auth("outsider")), 404)
    check(
        client.post(
            base,
            files={"file": ("x.docx", b"SYNTHETIC", "application/msword")},
            headers=world.auth("analyst"),
        ),
        415,
    )
    check(
        client.post(
            base,
            data=form,
            files={"other": ("a.csv", b"a", "text/csv")},
            headers=world.auth("analyst"),
        ),
        422,
    )
    ev = check(
        client.post(base, files=files, data=form, headers=world.auth("analyst")), 201, "Evidence"
    )
    assert ev["type"] == "csv" and ev["sha256"] == hashlib.sha256(data).hexdigest()
    assert [(j["stage"], j["status"]) for j in ev["ingest_jobs"]] == [("schema_infer", "queued")]
    assert ev["chain_of_custody"][0]["note"] == "SYNTHETIC test upload"
    assert ev["uploaded_by"] == str(world.users["analyst"]) and ev["recorded_at"] is not None
    check(client.post(base, files=files, headers=world.auth("lead")), 409)
    assert "evidence.upload" in audit_actions(world, world.case_id, "analyst")

    content = f"{base}/{ev['id']}/content"
    resp = client.get(content, headers=world.auth("viewer"), follow_redirects=False)
    assert resp.status_code == 307 and resp.headers["cache-control"] == "no-store"
    assert resp.headers["location"].startswith("https://storage.invalid/")
    check(client.get(content, headers=world.auth("outsider"), follow_redirects=False), 404)
    # tamper with the stored original: the file is never served
    key = next(k for k, v in world.store.store.objects.items() if v == data)
    world.store.store.objects[key] = data + b"tampered"
    check(client.get(content, headers=world.auth("viewer"), follow_redirects=False), 409)
    views = owner_rows(
        world.url,
        "SELECT payload ->> 'integrity_ok' FROM public.audit_log WHERE action = 'evidence.view' "
        "AND payload ->> 'evidence_id' = %s ORDER BY id",
        (ev["id"],),
    )
    assert [v[0] for v in views] == ["true", "false"]


# ---- timeline, entities, graph, conflicts --------------------------------------------------


def test_timeline_filters_pagination_and_audit(client: TestClient, world: World) -> None:
    url = f"/v1/cases/{world.case_id}/timeline"
    page = check(client.get(url, headers=world.auth("viewer")), 200, "TimelineEventList")
    starts = [e["t_start"] for e in page["items"]]
    assert len(starts) == 3 and starts == sorted(starts)
    sourced = next(e for e in page["items"] if e["sources"])
    assert sourced["sources"][0]["chunk_id"] == str(world.chunk_id)
    by_entity = check(
        client.get(
            url, params={"entity_id": str(world.entities["dale"])}, headers=world.auth("viewer")
        ),
        200,
        "TimelineEventList",
    )
    assert [e["description"] for e in by_entity["items"]] == ["SYNTHETIC: Mercer drives in."]
    ranged = check(
        client.get(
            url,
            params={"from": "2026-03-14T00:00:00Z", "to": "2026-03-16T00:00:00Z"},
            headers=world.auth("viewer"),
        ),
        200,
        "TimelineEventList",
    )
    assert len(ranged["items"]) == 2
    first = check(
        client.get(url, params={"limit": 2}, headers=world.auth("viewer")), 200, "TimelineEventList"
    )
    rest = check(
        client.get(
            url, params={"limit": 2, "cursor": first["next_cursor"]}, headers=world.auth("viewer")
        ),
        200,
        "TimelineEventList",
    )
    assert [e["id"] for e in first["items"] + rest["items"]] == [e["id"] for e in page["items"]]
    assert rest["next_cursor"] is None
    assert audit_actions(world, world.case_id, "viewer").count("timeline.view") >= 4
    check(client.get(url, headers=world.auth("outsider")), 404)


def test_entities_search_and_detail(client: TestClient, world: World) -> None:
    url = f"/v1/cases/{world.case_id}/entities"
    allp = check(client.get(url, headers=world.auth("viewer")), 200, "EntityList")
    assert len(allp["items"]) == 4
    found = check(
        client.get(url, params={"q": "Mercer"}, headers=world.auth("viewer")), 200, "EntityList"
    )
    assert found["items"][0]["canonical_name"] == "Dale Mercer"
    people = check(
        client.get(url, params={"type": "person", "limit": 1}, headers=world.auth("viewer")),
        200,
        "EntityList",
    )
    more = check(
        client.get(
            url,
            params={"type": "person", "cursor": people["next_cursor"]},
            headers=world.auth("viewer"),
        ),
        200,
        "EntityList",
    )
    assert {e["canonical_name"] for e in people["items"] + more["items"]} == {"Dale", "Dale Mercer"}
    detail = check(
        client.get(f"{url}/{world.entities['mercer']}", headers=world.auth("viewer")),
        200,
        "EntityDetail",
    )
    assert detail["mention_count"] == 1 and detail["mentions"][0]["locator"]["page"] == 2
    assert "D. Mercer" in detail["aliases"] and "Dale" in detail["aliases"]
    assert [link["id"] for link in detail["links"]] == [str(world.link_id)]
    check(client.get(f"{url}/{uuid.uuid4()}", headers=world.auth("viewer")), 404)
    check(client.get(url, params={"q": ""}, headers=world.auth("viewer")), 422)


def test_graph(client: TestClient, world: World) -> None:
    url = f"/v1/cases/{world.case_id}/graph"
    graph = check(client.get(url, headers=world.auth("viewer")), 200, "Graph")
    assert len(graph["nodes"]) == 4
    kinds = {(e["kind"], e["link_id"]) for e in graph["edges"]}
    assert ("same_as", str(world.link_id)) in kinds
    assert sum(1 for e in graph["edges"] if e["kind"] == "co_mention") == 3
    people = check(
        client.get(
            url, params=[("type", "person"), ("type", "vehicle")], headers=world.auth("viewer")
        ),
        200,
        "Graph",
    )
    assert {n["type"] for n in people["nodes"]} == {"person", "vehicle"}
    check(client.get(url, params={"type": "planet"}, headers=world.auth("viewer")), 422)


def test_revert_entity_link(client: TestClient, world: World) -> None:
    link_id = uuid.uuid4()
    import psycopg

    with psycopg.connect(libpq_url(world.url), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO public.entity_links (id, case_id, from_entity_id, to_entity_id, method,"
            " confidence) VALUES (%s, %s, %s, %s, 'llm', 0.7)",
            (link_id, world.case_id, world.entities["sedan"], world.entities["lot"]),
        )
    url = f"/v1/cases/{world.case_id}/entity-links/{link_id}/revert"
    check(client.post(url, headers=world.auth("viewer")), 403)
    check(client.post(url, headers=world.auth("outsider")), 404)
    check(
        client.post(
            f"/v1/cases/{world.case_id}/entity-links/{uuid.uuid4()}/revert",
            headers=world.auth("analyst"),
        ),
        404,
    )
    link = check(client.post(url, headers=world.auth("analyst")), 200, "EntityLink")
    assert link["status"] == "reverted" and link["reverted_by"] == str(world.users["analyst"])
    check(client.post(url, headers=world.auth("lead")), 409)
    assert "entity_link.revert" in audit_actions(world, world.case_id, "analyst")


def test_conflicts(client: TestClient, world: World) -> None:
    url = f"/v1/cases/{world.case_id}/conflicts"
    page = check(client.get(url, headers=world.auth("viewer")), 200, "ConflictList")
    assert page["items"] and page["items"][0]["claim_a"]["source_refs"][0]["source_type"] == "chunk"
    reviewed = check(
        client.get(url, params={"status": "reviewed"}, headers=world.auth("viewer")),
        200,
        "ConflictList",
    )
    assert reviewed["items"] == []


# ---- agent ---------------------------------------------------------------------------------


def _fake_agent(world: World) -> dict[str, Any]:
    chunk = evidence_result(
        STATEMENT, page=2, chunk_id=world.chunk_id, evidence_id=world.evidence_id
    )
    model = ScriptedModel(
        plan=[tool_call("search_evidence", {"query": "who drove the gray sedan"})],
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
                "conflicts": [],
                "confidence": 0.8,
                "open_questions": [],
            }
        ],
    )
    return {
        "router": model.router(),
        "backends": FakeBackends(evidence=[chunk]).backends(),
        "config": AgentConfig(prefetch=False),  # exactly the scripted tool calls
    }


def test_ask_runs_and_rate_limit(client: TestClient, world: World) -> None:
    svc = services(client)
    svc.ask_kwargs = _fake_agent(world)
    url = f"/v1/cases/{world.case_id}/ask"
    q = {"question": "Who was in the gray sedan on March 14?"}
    check(client.post(url, json=q), 401)
    check(client.post(url, json=q, headers=world.auth("outsider")), 404)
    check(client.post(url, json={"question": ""}, headers=world.auth("viewer")), 422)
    run = check(client.post(url, json=q, headers=world.auth("viewer")), 200, "AgentRun")
    assert run["status"] == "succeeded" and run["answer"]["status"] == "answered"
    assert run["answer"]["citations"][0]["chunk_id"] == str(world.chunk_id)
    assert [t["name"] for t in run["tool_calls"]] == ["search_evidence"]
    assert run["plan"] and run["user_id"] == str(world.users["viewer"])
    runs = check(
        client.get(f"/v1/cases/{world.case_id}/runs", headers=world.auth("viewer")),
        200,
        "AgentRunList",
    )
    assert runs["items"][0]["id"] == run["id"] and runs["items"][0]["answer_status"] == "answered"
    again = check(
        client.get(f"/v1/cases/{world.case_id}/runs/{run['id']}", headers=world.auth("lead")),
        200,
        "AgentRun",
    )
    assert again["answer"] == run["answer"]
    check(
        client.get(f"/v1/cases/{world.case_id}/runs/{run['id']}", headers=world.auth("outsider")),
        404,
    )
    actions = audit_actions(world, world.case_id, "viewer")
    assert "agent.ask" in actions and "agent.tool_call" in actions

    original = svc.ask_limiter
    svc.ask_limiter = RateLimiter(0, 60)
    try:
        resp = client.post(url, json=q, headers=world.auth("viewer"))
        check(resp, 429)
        assert int(resp.headers["retry-after"]) >= 1
    finally:
        svc.ask_limiter = original


# ---- audit ---------------------------------------------------------------------------------


def test_case_audit_lead_only_and_chain_fields(client: TestClient, world: World) -> None:
    url = f"/v1/cases/{world.case_id}/audit"
    check(client.get(url, headers=world.auth("analyst")), 403)
    check(client.get(url, headers=world.auth("viewer")), 403)
    check(client.get(url, headers=world.auth("outsider")), 404)
    page = check(
        client.get(url, params={"limit": 2}, headers=world.auth("lead")), 200, "AuditEntryList"
    )
    nxt = check(
        client.get(
            url, params={"limit": 2, "cursor": page["next_cursor"]}, headers=world.auth("lead")
        ),
        200,
        "AuditEntryList",
    )
    ids = [e["id"] for e in page["items"] + nxt["items"]]
    assert ids == sorted(ids) and len(set(ids)) == 4
    only = check(
        client.get(url, params={"action": "case.update"}, headers=world.auth("lead")),
        200,
        "AuditEntryList",
    )
    assert only["items"] and {e["action"] for e in only["items"]} == {"case.update"}
    assert all(e["case_id"] == str(world.case_id) for e in only["items"])
    assert "audit.view" in audit_actions(world, world.case_id, "lead")


def test_admin_audit_and_verify(client: TestClient, world: World) -> None:
    check(client.get("/v1/admin/audit", headers=world.auth("lead")), 403)
    check(client.get("/v1/admin/audit/verify", headers=world.auth("lead")), 403)
    check(client.get("/v1/admin/audit"), 401)
    page = check(
        client.get(
            "/v1/admin/audit",
            params={"case_id": str(world.other_case_id)},
            headers=world.auth("admin"),
        ),
        200,
        "AuditEntryList",
    )
    assert all(e["case_id"] == str(world.other_case_id) for e in page["items"])
    result = check(
        client.get("/v1/admin/audit/verify", headers=world.auth("admin")), 200, "AuditVerification"
    )
    assert result["valid"] is True and result["first_invalid_id"] is None
    assert audit_actions(world, None, "admin") == ["admin.audit.view", "admin.audit.verify"]


# ---- SSE -----------------------------------------------------------------------------------


def _frames(text: str) -> list[dict[str, str]]:
    frames = []
    for block in text.split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            if line.startswith(":") or ":" not in line:
                continue
            name, _, value = line.partition(":")
            fields[name] = value.strip()
        if "event" in fields:
            frames.append(fields)
    return frames


def test_ingest_events_stream(client: TestClient, world: World) -> None:
    url = f"/v1/cases/{world.case_id}/ingest/events"
    check(client.get(url, headers=world.auth("outsider")), 404)
    check(client.get(url), 401)

    def flip() -> None:
        import psycopg

        with psycopg.connect(libpq_url(world.url), autocommit=True) as conn:
            for status, delay in (("running", 0.15), ("succeeded", 0.2)):
                time.sleep(delay)
                conn.execute(
                    "UPDATE public.ingest_jobs SET status = %s, updated_at = clock_timestamp() "
                    "WHERE evidence_id = %s AND stage = 'parse'",
                    (status, world.evidence_id),
                )

    worker = threading.Thread(target=flip)
    worker.start()
    resp = client.get(url, headers={**world.auth("viewer"), "Last-Event-ID": "41"})
    worker.join()
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert ": heartbeat" in resp.text
    frames = _frames(resp.text)
    ids = [int(f["id"]) for f in frames]
    assert ids[0] == 42 and ids == sorted(ids) and len(set(ids)) == len(ids)
    for frame in frames:
        data = json.loads(frame["data"])
        assert schema_errors(data, "IngestEvent") == [] and data["event"] == frame["event"]
    events = [(f["event"], json.loads(f["data"])) for f in frames]
    statuses = [
        d["job"]["status"] for e, d in events if e == "job.updated" and d["job"]["stage"] == "parse"
    ]
    assert statuses[:1] == ["succeeded"] and "running" in statuses and statuses[-1] == "succeeded"
    assert any(
        e == "evidence.ready" and d["evidence_id"] == str(world.evidence_id) for e, d in events
    )


# ---- security review fixes -----------------------------------------------------------------


def _throwaway_login(url: str) -> tuple[str, str]:
    """Create a SYNTHETIC NOSUPERUSER NOBYPASSRLS login shaped like caselens_api_login; return
    (role name, conninfo)."""
    name = f"caselens_test_login_{uuid.uuid4().hex[:8]}"
    password = uuid.uuid4().hex
    with psycopg.connect(libpq_url(url), autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT PASSWORD {}").format(
                sql.Identifier(name), sql.Literal(password)
            )
        )
        conn.execute(
            sql.SQL("GRANT caselens_app, caselens_readonly TO {}").format(sql.Identifier(name))
        )
    return name, make_conninfo(libpq_url(url), user=name, password=password)


def _drop_login(url: str, name: str) -> None:
    with psycopg.connect(libpq_url(url), autocommit=True) as conn:
        conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name)))


def test_api_pool_uses_dedicated_non_superuser_login(world: World) -> None:
    name, login_url = _throwaway_login(world.url)
    try:
        settings = Settings(
            environment="prod",
            database_url=world.url,
            api_database_url=login_url,
            warm_up_models=False,
        )
        app = create_app(settings, verifier=CompositeVerifier(None, None))
        with TestClient(app) as test_client:  # the strict startup check passes
            db = services(test_client).require_db()
            info = db.login_info()
            assert (info.name, info.superuser, info.bypass_rls) == (name, False, False)
            with db.session(world.users["lead"]) as conn:
                row = conn.execute(
                    "SELECT session_user, current_user, (SELECT count(*) FROM public.cases)"
                ).fetchone()
                assert row is not None and row[:2] == (name, "caselens_app") and row[2] >= 1
            with db.pool.connection() as raw, pytest.raises(psycopg.errors.InsufficientPrivilege):
                raw.execute("SELECT count(*) FROM public.cases")  # no privileges as itself
    finally:
        _drop_login(world.url, name)


def test_superuser_login_refused_outside_local(world: World) -> None:
    rolsuper = owner_rows(
        world.url, "SELECT rolsuper FROM pg_roles WHERE rolname = session_user", ()
    )[0][0]
    assert rolsuper, "the test database URL is expected to be the owner superuser"
    app = create_app(
        Settings(
            environment="prod",
            database_url=world.url,
            api_database_url=None,
            warm_up_models=False,
        ),
        verifier=CompositeVerifier(None, None),
    )
    with pytest.raises(RuntimeError, match="superuser"), TestClient(app):
        pass
    local = create_app(
        Settings(
            environment="local",
            database_url=world.url,
            api_database_url=None,
            warm_up_models=False,
        ),
        verifier=CompositeVerifier(None, None),
    )
    with TestClient(local) as test_client:  # local: warns and starts
        assert check(test_client.get("/healthz"), 200, "Health")["status"] == "ok"


def test_ask_concurrency_bound_returns_429(client: TestClient, world: World) -> None:
    svc = services(client)
    svc.ask_kwargs = _fake_agent(world)
    original = svc.ask_slots
    svc.ask_slots = threading.BoundedSemaphore(1)
    url = f"/v1/cases/{world.case_id}/ask"
    q = {"question": "Who was in the gray sedan on March 14?"}
    try:
        assert svc.ask_slots.acquire(blocking=False)  # one run in flight
        resp = client.post(url, json=q, headers=world.auth("analyst"))
        check(resp, 429)
        assert int(resp.headers["retry-after"]) >= 1
        svc.ask_slots.release()
        check(client.post(url, json=q, headers=world.auth("analyst")), 200, "AgentRun")
        assert svc.ask_slots.acquire(blocking=False)  # the slot was released after the run
        svc.ask_slots.release()
    finally:
        svc.ask_slots = original


def test_demo_users_see_only_their_runs_and_are_limited_per_ip(
    client: TestClient, world: World
) -> None:
    svc = services(client)
    svc.ask_kwargs = _fake_agent(world)
    base = f"/v1/cases/{world.case_id}"
    q = {"question": "Who was in the gray sedan on March 14?"}
    other = check(client.post(f"{base}/ask", json=q, headers=world.auth("viewer")), 200, "AgentRun")
    original_limiter, original_hops = svc.ask_limiter, svc.settings.trusted_proxy_hops
    svc.ask_limiter = RateLimiter(1, 60)
    try:
        demo = check(
            client.post(f"{base}/ask", json=q, headers=world.auth("demo")), 200, "AgentRun"
        )
        # same client address -> limited (the demo login is shared, so the key is the address)
        check(client.post(f"{base}/ask", json=q, headers=world.auth("demo")), 429)
        # forwarded addresses are ignored unless proxies are trusted ...
        spoof = {**world.auth("demo"), "X-Forwarded-For": "203.0.113.10"}
        check(client.post(f"{base}/ask", json=q, headers=spoof), 429)
        # ... and with one trusted proxy each client address has its own budget
        svc.settings.trusted_proxy_hops = 1
        check(client.post(f"{base}/ask", json=q, headers=spoof), 200, "AgentRun")
        # non-demo users keep their per-user budget
        check(client.post(f"{base}/ask", json=q, headers=world.auth("analyst")), 200, "AgentRun")
    finally:
        svc.ask_limiter, svc.settings.trusted_proxy_hops = original_limiter, original_hops

    mine = check(client.get(f"{base}/runs", headers=world.auth("demo")), 200, "AgentRunList")
    assert mine["items"] and {r["user_id"] for r in mine["items"]} == {str(world.users["demo"])}
    assert demo["id"] in {r["id"] for r in mine["items"]}
    check(client.get(f"{base}/runs/{other['id']}", headers=world.auth("demo")), 404)
    check(client.get(f"{base}/runs/{demo['id']}", headers=world.auth("demo")), 200, "AgentRun")
    everyone = check(client.get(f"{base}/runs", headers=world.auth("viewer")), 200, "AgentRunList")
    users = {r["user_id"] for r in everyone["items"]}
    assert {str(world.users["demo"]), str(world.users["viewer"])} <= users
    check(client.get(f"{base}/runs/{demo['id']}", headers=world.auth("viewer")), 200, "AgentRun")


def test_demo_user_cannot_be_promoted(client: TestClient, world: World) -> None:
    case_id = new_case(client, world, "SYNTHETIC demo-role case")
    base = f"/v1/cases/{case_id}/members"
    lead = world.auth("lead")
    demo = str(world.users["demo"])
    for role in ("analyst", "lead"):
        check(client.post(base, json={"user_id": demo, "role": role}, headers=lead), 403)
    check(
        client.post(base, json={"user_id": demo, "role": "viewer"}, headers=lead),
        201,
        "CaseMember",
    )
    for role in ("analyst", "lead"):
        check(client.patch(f"{base}/{demo}", json={"role": role}, headers=lead), 403)
    roles = owner_rows(
        world.url,
        "SELECT role FROM public.case_members WHERE case_id = %s AND user_id = %s",
        (uuid.UUID(case_id), world.users["demo"]),
    )
    assert roles == [("viewer",)]


def _multipart_chunks(boundary: str, size: int) -> Iterator[bytes]:
    yield (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
        'filename="SYNTHETIC_big.csv"\r\nContent-Type: text/csv\r\n\r\n'
    ).encode()
    chunk = b"SYNTHETIC,row\n" * 4096
    sent = 0
    while sent < size:
        yield chunk
        sent += len(chunk)
    yield f"\r\n--{boundary}--\r\n".encode()


def test_upload_limit_enforced_while_streaming(client: TestClient, world: World) -> None:
    svc = services(client)
    original = svc.settings.max_upload_bytes
    svc.settings.max_upload_bytes = 1024
    boundary = "SYNTHETICboundary" + uuid.uuid4().hex
    count = "SELECT count(*) FROM public.evidence WHERE case_id = %s"
    before = owner_rows(world.url, count, (world.case_id,))
    try:
        resp = client.post(
            f"/v1/cases/{world.case_id}/evidence",
            content=_multipart_chunks(boundary, 512 * 1024),  # chunked: no Content-Length
            headers={
                **world.auth("analyst"),
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )
        assert "content-length" not in {k.lower() for k in resp.request.headers}
        check(resp, 413)
    finally:
        svc.settings.max_upload_bytes = original
    assert owner_rows(world.url, count, (world.case_id,)) == before


def test_content_hashes_without_holding_a_connection(
    client: TestClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = services(client).require_db().pool
    in_use: list[int] = []
    real = world.store.sha256

    def spy(uri: str) -> str:
        # Every pool connection can be checked out while the object is hashed: the request
        # holds none (a held one would make the last getconn time out).
        held = [pool.getconn(timeout=3) for _ in range(pool.max_size)]
        for conn in held:
            pool.putconn(conn)
        in_use.append(pool.max_size - len(held))
        return real(uri)

    monkeypatch.setattr(world.store, "sha256", spy)
    url = f"/v1/cases/{world.case_id}/evidence/{world.evidence_id}/content"
    resp = client.get(url, headers=world.auth("viewer"), follow_redirects=False)
    assert resp.status_code == 307
    assert in_use == [0]
    check(client.get(url, headers=world.auth("outsider"), follow_redirects=False), 404)
    check(client.get(url, follow_redirects=False), 401)
    missing = f"/v1/cases/{world.case_id}/evidence/{uuid.uuid4()}/content"
    check(client.get(missing, headers=world.auth("viewer"), follow_redirects=False), 404)
    assert in_use == [0]  # rejected requests never reach the store
    rows = owner_rows(
        world.url,
        "SELECT user_id, payload ->> 'integrity_ok' FROM public.audit_log "
        "WHERE action = 'evidence.view' AND payload ->> 'evidence_id' = %s ORDER BY id",
        (str(world.evidence_id),),
    )
    assert rows[-1] == (world.users["viewer"], "true")
