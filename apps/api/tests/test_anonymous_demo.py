"""Anonymous Firebase visitors as the shared demo viewer (D-066), against Postgres, with a mocked
Firebase verifier: off -> 401; on -> the `demo:viewer` user with the demo restrictions, each
visitor seeing only its own runs; evidence content through the local-disk store's signed URLs.
SYNTHETIC data only."""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from caselens_agent.loop import AgentConfig
from caselens_agent.testing import FakeBackends, ScriptedModel, evidence_result, tool_call
from caselens_api.auth import DEMO_VIEWER_SUBJECT, AuthError, DevTokenVerifier, TokenIdentity
from caselens_api.deps import Services
from caselens_api.main import create_app
from caselens_api.settings import Settings
from caselens_api.storage import LocalDiskEvidenceStore, MemoryEvidenceStore
from caselens_api.testing import DEV_SECRET, PDF_BYTES, STATEMENT, World, check, seed_world
from caselens_workers.settings import libpq_url
from caselens_workers.storage import evidence_key

pytestmark = pytest.mark.db

SIGNING_SECRET = "SYNTHETIC-anon-demo-signing-secret-0123456789"  # noqa: S105 - test-only
ANON_PREFIX = "anon:"


class FakeFirebase:
    """Stands in for FirebaseTokenVerifier: `anon:<uid>` is a verified anonymous token;
    anything else goes to the dev verifier (the world's named users)."""

    def __init__(self) -> None:
        self.dev = DevTokenVerifier(DEV_SECRET)

    def verify(self, token: str) -> TokenIdentity:
        if token.startswith(ANON_PREFIX):
            uid = token.removeprefix(ANON_PREFIX)
            if not uid:
                raise AuthError("no uid")
            return TokenIdentity(subject=uid, sign_in_provider="anonymous")
        return self.dev.verify(token)


def anon(uid: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {ANON_PREFIX}{uid}"}


@pytest.fixture(scope="module")
def world(migrated_url: str) -> World:
    world = seed_world(migrated_url, MemoryEvidenceStore())
    with psycopg.connect(libpq_url(migrated_url), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO public.users (oidc_subject, display_name, is_demo) "
            "VALUES (%s, 'SYNTHETIC demo viewer', true) "
            "ON CONFLICT (oidc_subject) DO UPDATE SET is_demo = true",
            (DEMO_VIEWER_SUBJECT,),
        )
        row = conn.execute(
            "SELECT id FROM public.users WHERE oidc_subject = %s", (DEMO_VIEWER_SUBJECT,)
        ).fetchone()
        assert row is not None
        conn.execute(
            "INSERT INTO public.case_members (case_id, user_id, role) VALUES (%s, %s, 'viewer')",
            (world.case_id, row[0]),
        )
    world.users["demo_viewer"] = row[0]
    return world


@pytest.fixture(scope="module")
def disk(world: World, tmp_path_factory: pytest.TempPathFactory) -> LocalDiskEvidenceStore:
    root: Path = tmp_path_factory.mktemp("objects")
    store = LocalDiskEvidenceStore(root, world.store.store.bucket, SIGNING_SECRET.encode())
    key = evidence_key(world.case_id, world.evidence_id)
    store.put_stream(key, io.BytesIO(PDF_BYTES), len(PDF_BYTES), "application/pdf")
    return store


def _client(world: World, disk: LocalDiskEvidenceStore, *, anonymous_demo: bool) -> TestClient:
    settings = Settings(
        environment="local",
        database_url=world.url,
        api_database_url=None,
        db_pool_max_size=6,
        warm_up_models=False,
        anonymous_demo=anonymous_demo,
        storage_backend="local",
        local_storage_root=disk.root,
        storage_signing_secret=SecretStr(SIGNING_SECRET),
    )
    return TestClient(create_app(settings, verifier=FakeFirebase(), store_factory=lambda: disk))


@pytest.fixture(scope="module")
def client(world: World, disk: LocalDiskEvidenceStore) -> Iterator[TestClient]:
    with _client(world, disk, anonymous_demo=True) as test_client:
        yield test_client


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
        "config": AgentConfig(prefetch=False),
    }


def test_anonymous_tokens_are_rejected_when_the_demo_is_off(
    world: World, disk: LocalDiskEvidenceStore
) -> None:
    with _client(world, disk, anonymous_demo=False) as off:
        check(off.get("/v1/me", headers=anon("SYNTHETIC-visitor-off")), 401)
        check(off.get("/v1/cases", headers=anon("SYNTHETIC-visitor-off")), 401)
        check(off.get("/v1/me", headers=world.auth("viewer")), 200, "Me")
    with psycopg.connect(libpq_url(world.url)) as conn:
        row = conn.execute(
            "SELECT count(*) FROM public.users WHERE oidc_subject = 'SYNTHETIC-visitor-off'"
        ).fetchone()
    assert row == (0,)


def test_anonymous_visitors_are_the_shared_demo_viewer(client: TestClient, world: World) -> None:
    me = check(client.get("/v1/me", headers=anon("SYNTHETIC-visitor-a")), 200, "Me")
    assert me["user"]["id"] == str(world.users["demo_viewer"]) and me["user"]["is_demo"] is True
    other = check(client.get("/v1/me", headers=anon("SYNTHETIC-visitor-b")), 200, "Me")
    assert other["user"]["id"] == me["user"]["id"]
    with psycopg.connect(libpq_url(world.url)) as conn:
        row = conn.execute(
            "SELECT count(*) FROM public.users WHERE oidc_subject LIKE 'SYNTHETIC-visitor-%'"
        ).fetchone()
    assert row == (0,)  # no users row per anonymous uid
    case = check(
        client.get(f"/v1/cases/{world.case_id}", headers=anon("SYNTHETIC-visitor-a")), 200, "Case"
    )
    assert case["my_role"] == "viewer"
    check(client.get(f"/v1/cases/{world.other_case_id}", headers=anon("SYNTHETIC-visitor-a")), 404)
    check(client.post("/v1/cases", json={"name": "x"}, headers=anon("SYNTHETIC-visitor-a")), 403)
    files = {"file": ("s.pdf", PDF_BYTES, "application/pdf")}
    check(
        client.post(
            f"/v1/cases/{world.case_id}/evidence", files=files, headers=anon("SYNTHETIC-visitor-a")
        ),
        403,
    )


def test_visitors_only_see_their_own_runs(client: TestClient, world: World) -> None:
    svc: Services = client.app.state.services  # type: ignore[attr-defined]
    svc.ask_kwargs = _fake_agent(world)
    base = f"/v1/cases/{world.case_id}"
    q = {"question": "Who was in the gray sedan on March 14?"}
    a, b = anon("SYNTHETIC-visitor-runs-a"), anon("SYNTHETIC-visitor-runs-b")
    run_a = check(client.post(f"{base}/ask", json=q, headers=a), 200, "AgentRun")
    assert run_a["user_id"] == str(world.users["demo_viewer"])
    assert all(c.get("conflict_record_id") is None for c in run_a["answer"]["conflicts"])
    check(client.get(f"{base}/runs", headers=b), 200, "AgentRunList")
    assert client.get(f"{base}/runs", headers=b).json()["items"] == []
    check(client.get(f"{base}/runs/{run_a['id']}", headers=b), 404)
    mine = check(client.get(f"{base}/runs", headers=a), 200, "AgentRunList")
    assert [r["id"] for r in mine["items"]] == [run_a["id"]]
    check(client.get(f"{base}/runs/{run_a['id']}", headers=a), 200, "AgentRun")
    run_b = check(client.post(f"{base}/ask", json=q, headers=b), 200, "AgentRun")
    assert [r["id"] for r in client.get(f"{base}/runs", headers=b).json()["items"]] == [run_b["id"]]
    # Named members still see every run, including the visitors'.
    everyone = check(client.get(f"{base}/runs", headers=world.auth("lead")), 200, "AgentRunList")
    assert {run_a["id"], run_b["id"]} <= {r["id"] for r in everyone["items"]}


def test_content_redirects_to_a_signed_local_url(client: TestClient, world: World) -> None:
    url = f"/v1/cases/{world.case_id}/evidence/{world.evidence_id}/content"
    redirect = client.get(url, headers=anon("SYNTHETIC-visitor-c"), follow_redirects=False)
    assert redirect.status_code == 307, redirect.text
    location = redirect.headers["location"]
    assert location.startswith("/storage/")
    body = client.get(location)
    assert body.status_code == 200 and body.content == PDF_BYTES
    assert body.headers["content-type"] == "application/pdf"
    part = client.get(location, headers={"Range": "bytes=0-7"})
    assert part.status_code == 206 and part.content == PDF_BYTES[:8]
    assert client.get(location[:-4] + "AAAA").status_code == 403
