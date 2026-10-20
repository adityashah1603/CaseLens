"""Shared pytest fixtures for every package.

DB tests run against CASELENS_TEST_DATABASE_URL, migrated to head, each inside a transaction that
is rolled back, with SYNTHETIC rows only."""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg.types.json import Jsonb

from caselens_workers.db import Conn
from caselens_workers.settings import WorkerSettings, libpq_url
from caselens_workers.storage import MemoryObjectStore, evidence_key
from caselens_workers.testing import SeededEvidence, as_role

API_DIR = Path(__file__).resolve().parent / "apps" / "api"


@pytest.fixture(scope="session")
def migrated_url() -> str:
    url = os.environ.get("CASELENS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("CASELENS_TEST_DATABASE_URL is not set")
    cfg = Config(str(API_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    cfg.attributes["configure_logger"] = False
    os.environ.setdefault("CASELENS_DATABASE_URL", url)
    command.upgrade(cfg, "head")
    return url


@pytest.fixture
def owner_conn(migrated_url: str) -> Iterator[Conn]:
    """Owner connection in a rolled-back transaction. Use `as_role` to drop privileges."""
    with (
        psycopg.connect(libpq_url(migrated_url), autocommit=True) as conn,
        conn.transaction(force_rollback=True),
    ):
        yield conn


@pytest.fixture
def seed_evidence(owner_conn: Conn) -> Callable[..., SeededEvidence]:
    """Insert a SYNTHETIC user, case and one evidence file (as owner) and store its bytes."""

    def make(
        path: Path,
        evidence_type: str,
        recorded_at: str | None = None,
        store: MemoryObjectStore | None = None,
    ) -> SeededEvidence:
        import hashlib

        store = store or MemoryObjectStore()
        user_id, case_id, evidence_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        as_role(owner_conn, None)
        owner_conn.execute(
            "INSERT INTO public.users (id, oidc_subject) VALUES (%s, %s)",
            (user_id, f"test:{user_id}"),
        )
        owner_conn.execute(
            "INSERT INTO public.cases (id, name, created_by) VALUES (%s, %s, %s)",
            (case_id, "SYNTHETIC test case", user_id),
        )
        uri = store.put_file(evidence_key(case_id, evidence_id), path, "application/octet-stream")
        owner_conn.execute(
            "INSERT INTO public.evidence (id, case_id, type, original_filename, content_type, "
            "size_bytes, gcs_uri, sha256, uploaded_by, recorded_at, chain_of_custody) "
            "VALUES (%s, %s, %s, %s, 'application/octet-stream', %s, %s, %s, %s, %s, %s)",
            (
                evidence_id,
                case_id,
                evidence_type,
                path.name,
                path.stat().st_size,
                uri,
                hashlib.sha256(path.read_bytes()).hexdigest(),
                user_id,
                recorded_at,
                Jsonb([]),
            ),
        )
        return SeededEvidence(case_id, evidence_id, user_id, store)

    return make


@pytest.fixture
def worker_settings() -> WorkerSettings:
    return WorkerSettings(worker_db_role=None, app_db_role=None, max_attempts=3)
