"""API test fixtures. The API commits through pooled connections, so seeded rows are not rolled
back: every module seeds a fresh SYNTHETIC world with new UUIDs. Use a throwaway database
(CASELENS_TEST_DATABASE_URL)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from caselens_api.main import create_app
from caselens_api.settings import Settings
from caselens_api.storage import MemoryEvidenceStore
from caselens_api.testing import DEV_SECRET, World, seed_world


@pytest.fixture(scope="module")
def world(migrated_url: str) -> World:
    return seed_world(migrated_url, MemoryEvidenceStore())


@pytest.fixture(scope="module")
def client(world: World) -> Iterator[TestClient]:
    settings = Settings(
        environment="local",
        database_url=world.url,
        dev_auth_secret=SecretStr(DEV_SECRET),
        db_pool_max_size=4,
        sse_poll_s=0.05,
        sse_heartbeat_s=0.05,
        sse_max_stream_s=0.6,
    )
    app = create_app(settings, store_factory=lambda: world.store)
    with TestClient(app) as test_client:
        yield test_client
