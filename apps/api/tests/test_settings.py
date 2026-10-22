"""Fail-closed settings: deployed by default, no dev tokens outside local, pool sizing, client
addresses for the demo /ask limiter. No database needed. SYNTHETIC values only."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError
from starlette.requests import Request

from caselens_api.auth import build_verifier
from caselens_api.deps import RateLimiter, client_ip
from caselens_api.main import create_app
from caselens_api.settings import Settings

DEV_SECRET = "SYNTHETIC-settings-test-secret-0123456789"  # noqa: S105 - test-only

_ENV = (
    "CASELENS_ENVIRONMENT",
    "CASELENS_DATABASE_URL",
    "CASELENS_API_DATABASE_URL",
    "CASELENS_DEV_AUTH_SECRET",
    "CASELENS_FIREBASE_PROJECT_ID",
    "CASELENS_DB_POOL_MAX_SIZE",
    "CASELENS_ASK_MAX_CONCURRENT",
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_environment_defaults_to_prod(clean_env: pytest.MonkeyPatch) -> None:
    settings = Settings()
    assert settings.environment == "prod" and not settings.is_local


def test_dev_secret_without_environment_refuses_to_start(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("CASELENS_DEV_AUTH_SECRET", DEV_SECRET)
    with pytest.raises(RuntimeError, match="only allowed locally"):
        create_app(Settings())


def test_prod_with_dev_secret_refuses_to_start(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("CASELENS_ENVIRONMENT", "prod")
    clean_env.setenv("CASELENS_DEV_AUTH_SECRET", DEV_SECRET)
    with pytest.raises(RuntimeError, match="only allowed locally"):
        create_app(Settings())


def test_dev_verifier_refused_with_firebase_outside_local() -> None:
    settings = Settings(
        environment="staging",
        firebase_project_id="caselens-synthetic",
        dev_auth_secret=SecretStr(DEV_SECRET),
    )
    with pytest.raises(RuntimeError, match="only allowed locally"):
        build_verifier(settings)


def test_local_dev_verifier_still_works(clean_env: pytest.MonkeyPatch) -> None:
    verifier = build_verifier(Settings(environment="local", dev_auth_secret=SecretStr(DEV_SECRET)))
    assert verifier.dev is not None and verifier.firebase is None


def test_pool_is_sized_for_concurrent_asks(clean_env: pytest.MonkeyPatch) -> None:
    assert Settings().pool_max_size >= 2 * Settings().ask_max_concurrent + 2
    assert Settings(ask_max_concurrent=3).pool_max_size == 10
    assert Settings(ask_max_concurrent=2, db_pool_max_size=5).pool_max_size == 5
    with pytest.raises(ValidationError, match="DB_POOL_MAX_SIZE"):
        Settings(ask_max_concurrent=2, db_pool_max_size=4)
    with pytest.raises(ValidationError, match="ASK_MAX_CONCURRENT"):
        Settings(ask_max_concurrent=0)


def test_api_pool_prefers_the_dedicated_login(clean_env: pytest.MonkeyPatch) -> None:
    owner = "postgresql://owner@db.invalid/caselens"
    api = "postgresql://caselens_api_login@db.invalid/caselens"
    assert Settings(database_url=owner).pool_url == owner
    assert Settings(database_url=owner, api_database_url=api).pool_url == api


def _request(peer: str, forwarded: list[str]) -> Request:
    headers = [(b"x-forwarded-for", value.encode()) for value in forwarded]
    return Request({"type": "http", "headers": headers, "client": (peer, 1234)})


def test_client_ip_trusts_forwarded_for_only_when_configured() -> None:
    spoofed = _request("10.0.0.9", ["203.0.113.66, 198.51.100.7"])
    assert client_ip(spoofed, 0) == "10.0.0.9"
    assert client_ip(spoofed, 1) == "198.51.100.7"
    assert client_ip(spoofed, 2) == "203.0.113.66"
    assert client_ip(spoofed, 3) == "10.0.0.9"  # fewer entries than proxies: the peer
    split = _request("10.0.0.9", ["203.0.113.66", "198.51.100.7"])
    assert client_ip(split, 1) == "198.51.100.7"


def test_rate_limiter_keys_are_independent() -> None:
    limiter = RateLimiter(1, 60)
    assert limiter.check(("ip", "198.51.100.7")) is None
    assert limiter.check(("ip", "198.51.100.7")) is not None
    assert limiter.check(("ip", "198.51.100.8")) is None
