"""Single-container demo pieces (D-066), no database: the local-disk evidence store and its
HMAC-signed /storage URLs (incl. Range requests), the SPA mount, the settings that guard them,
and the Firebase sign-in provider claim. SYNTHETIC bytes and secrets only."""

from __future__ import annotations

import io
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from caselens_api.auth import FirebaseTokenVerifier
from caselens_api.main import create_app
from caselens_api.settings import Settings
from caselens_api.storage import (
    InvalidStorageTokenError,
    LocalDiskEvidenceStore,
    StorageError,
    key_segments,
    sign_storage_token,
    verify_storage_token,
)

SECRET = b"SYNTHETIC-storage-signing-secret-0123456789"
KEY = "cases/c1/evidence/e1/original"
AUDIO = bytes(range(256)) * 64  # 16 KiB of SYNTHETIC "audio"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deployed-mode settings only: no developer CASELENS_* values (dev secret, DB URLs)."""
    import os

    for name in list(os.environ):
        if name.startswith("CASELENS_"):
            monkeypatch.delenv(name)


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(tmp_path: Path, clock: Clock) -> LocalDiskEvidenceStore:
    disk = LocalDiskEvidenceStore(tmp_path / "objects", "caselens-evidence", SECRET, clock=clock)
    disk.put_stream(KEY, io.BytesIO(AUDIO), len(AUDIO), "audio/wav")
    return disk


def _settings(tmp_path: Path, **extra: Any) -> Settings:
    return Settings(
        environment="prod",
        database_url=None,
        api_database_url=None,
        warm_up_models=False,
        storage_backend="local",
        local_storage_root=tmp_path / "objects",
        storage_signing_secret=SecretStr(SECRET.decode()),
        **extra,
    )


@pytest.fixture
def client(tmp_path: Path, store: LocalDiskEvidenceStore) -> Iterator[TestClient]:
    app = create_app(_settings(tmp_path), store_factory=lambda: store)
    with TestClient(app) as test_client:
        yield test_client


def _token(url: str) -> str:
    assert url.startswith("/storage/")
    return url.removeprefix("/storage/")


# ---- tokens --------------------------------------------------------------------------------


def test_token_round_trip_and_rejections() -> None:
    token = sign_storage_token(SECRET, KEY, 1000)
    assert verify_storage_token(SECRET, token, 999.0) == KEY
    assert verify_storage_token(SECRET, token, 1000.0) == KEY
    with pytest.raises(InvalidStorageTokenError, match="expired"):
        verify_storage_token(SECRET, token, 1000.5)
    with pytest.raises(InvalidStorageTokenError, match="signature"):
        verify_storage_token(b"SYNTHETIC-some-other-secret-0123456789", token, 0.0)
    payload, mac = token.split(".")
    forged = sign_storage_token(SECRET, "cases/c2/evidence/e2/original", 1000).split(".")[0]
    for bad in (f"{forged}.{mac}", f"{payload}.{mac[:-2]}", payload, "a.b.c", "", "x" * 3000):
        with pytest.raises(InvalidStorageTokenError):
            verify_storage_token(SECRET, bad, 0.0)


@pytest.mark.parametrize(
    "key",
    ["", "../etc/passwd", "cases/../../x", "/abs", "a//b", "a\\b", "C:/x", "a/./b", "x.meta.json"],
)
def test_unsafe_keys_are_refused(key: str, store: LocalDiskEvidenceStore) -> None:
    with pytest.raises(StorageError):
        key_segments(key)
    with pytest.raises(StorageError):
        store.path_for(key)


# ---- store ---------------------------------------------------------------------------------


def test_store_put_hash_and_sign(store: LocalDiskEvidenceStore, clock: Clock) -> None:
    import hashlib

    uri = f"s3://caselens-evidence/{KEY}"
    assert store.path_for(KEY).read_bytes() == AUDIO
    assert store.sha256(uri) == hashlib.sha256(AUDIO).hexdigest()
    url = store.signed_url(uri, 300)
    path, content_type = store.resolve_token(_token(url))
    assert path == store.path_for(KEY) and content_type == "audio/wav"
    clock.now += 301
    with pytest.raises(InvalidStorageTokenError):
        store.resolve_token(_token(url))
    with pytest.raises(StorageError):
        store.sha256("s3://another-bucket/" + KEY)
    with pytest.raises(StorageError):
        store.sha256("s3://caselens-evidence/cases/c1/evidence/missing/original")
    assert not list(store.path_for(KEY).parent.glob(".upload-*"))  # no temp files left


def test_short_upload_leaves_nothing(store: LocalDiskEvidenceStore) -> None:
    key = "cases/c1/evidence/e2/original"
    with pytest.raises(StorageError):
        store.put_stream(key, io.BytesIO(b"SYNTHETIC"), 100, "application/pdf")
    assert not store.path_for(key).exists()
    assert not list(store.path_for(key).parent.glob(".upload-*"))


def test_public_base_url_prefixes_signed_urls(tmp_path: Path) -> None:
    disk = LocalDiskEvidenceStore(tmp_path, "b", SECRET, "https://demo.example.test/")
    assert disk.signed_url("s3://b/" + KEY, 60).startswith("https://demo.example.test/storage/")


def test_local_backend_needs_root_and_long_secret(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="LOCAL_STORAGE_ROOT"):
        Settings(storage_backend="local", storage_signing_secret=SecretStr(SECRET.decode()))
    with pytest.raises(ValidationError, match="STORAGE_SIGNING_SECRET"):
        Settings(storage_backend="local", local_storage_root=tmp_path)
    with pytest.raises(ValidationError, match="STORAGE_SIGNING_SECRET"):
        Settings(
            storage_backend="local",
            local_storage_root=tmp_path,
            storage_signing_secret=SecretStr("short"),
        )
    assert Settings().storage_backend == "s3"


# ---- /storage route ------------------------------------------------------------------------


def test_storage_route_serves_ranges(client: TestClient, store: LocalDiskEvidenceStore) -> None:
    url = store.signed_url(f"s3://caselens-evidence/{KEY}", 300)
    full = client.get(url)
    assert full.status_code == 200 and full.content == AUDIO
    assert full.headers["content-type"] == "audio/wav"
    assert full.headers["cache-control"] == "no-store"
    assert full.headers["accept-ranges"] == "bytes"
    part = client.get(url, headers={"Range": "bytes=100-199"})
    assert part.status_code == 206 and part.content == AUDIO[100:200]
    assert part.headers["content-range"] == f"bytes 100-199/{len(AUDIO)}"
    tail = client.get(url, headers={"Range": "bytes=-10"})
    assert tail.status_code == 206 and tail.content == AUDIO[-10:]
    assert client.get(url, headers={"Range": f"bytes={len(AUDIO) + 5}-"}).status_code == 416
    head = client.head(url)
    assert head.status_code == 200 and head.headers["content-length"] == str(len(AUDIO))


def test_storage_route_refuses_bad_and_expired_tokens(
    client: TestClient, store: LocalDiskEvidenceStore, clock: Clock
) -> None:
    url = store.signed_url(f"s3://caselens-evidence/{KEY}", 300)
    for bad in (url[:-3] + "AAA", "/storage/not-a-token", "/storage/" + "x" * 3000):
        response = client.get(bad)
        assert response.status_code == 403, bad
        assert response.headers["content-type"].startswith("application/problem+json")
    clock.now += 301
    assert client.get(url).status_code == 403
    missing = sign_storage_token(SECRET, "cases/c1/evidence/gone/original", int(clock.now) + 60)
    assert client.get(f"/storage/{missing}").status_code == 404


def test_storage_route_only_exists_with_the_local_backend(tmp_path: Path) -> None:
    settings = Settings(environment="prod", warm_up_models=False, database_url=None)
    with TestClient(create_app(settings)) as plain:
        token = sign_storage_token(SECRET, KEY, int(time.time()) + 60)
        assert plain.get(f"/storage/{token}").status_code == 404


# ---- SPA -----------------------------------------------------------------------------------


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>SYNTHETIC</title>", encoding="utf-8")
    (root / "assets" / "index-abc123.js").write_text("console.log('SYNTHETIC')", encoding="utf-8")
    (root / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("outside dist", encoding="utf-8")
    return root


@pytest.fixture
def spa(tmp_path: Path, dist: Path, store: LocalDiskEvidenceStore) -> Iterator[TestClient]:
    app = create_app(_settings(tmp_path, web_dist=dist), store_factory=lambda: store)
    with TestClient(app) as test_client:
        yield test_client


def test_spa_serves_index_assets_and_client_routes(spa: TestClient) -> None:
    for path in ("/", "/cases/3f0c/ask", "/sign-in"):
        page = spa.get(path)
        assert page.status_code == 200 and "SYNTHETIC" in page.text, path
        assert page.headers["content-type"].startswith("text/html")
        assert page.headers["cache-control"] == "no-cache"
    asset = spa.get("/assets/index-abc123.js")
    assert asset.status_code == 200
    assert asset.headers["content-type"].startswith("text/javascript")
    assert "immutable" in asset.headers["cache-control"]
    assert spa.get("/favicon.svg").headers["content-type"] == "image/svg+xml"
    assert spa.head("/").status_code == 200
    assert spa.get("/assets/missing.js").status_code == 404
    assert spa.get("/..%2Fsecret.txt").status_code == 404


def test_spa_never_shadows_the_api(spa: TestClient, store: LocalDiskEvidenceStore) -> None:
    health = spa.get("/healthz")
    assert health.status_code == 200 and health.json()["status"] == "ok"
    for path in ("/v1/nope", "/v1", "/v1/cases", "/healthz/x", "/storage", "/storage/a/b"):
        response = spa.get(path)
        assert response.status_code in (401, 403, 404), path
        assert response.headers["content-type"].startswith("application/problem+json"), path
    url = store.signed_url(f"s3://caselens-evidence/{KEY}", 300)
    assert spa.get(url).content == AUDIO
    assert spa.post("/", json={}).status_code == 405


def test_spa_needs_a_built_index(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match=r"index\.html"):
        create_app(_settings(tmp_path, web_dist=tmp_path / "empty"))


# ---- Firebase sign-in provider -------------------------------------------------------------


def test_firebase_verifier_reports_the_sign_in_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = FirebaseTokenVerifier("caselens-synthetic", jwks_url="https://jwks.invalid/")
    signing = SimpleNamespace(key=key.public_key())
    monkeypatch.setattr(verifier._jwks, "get_signing_key_from_jwt", lambda _token: signing)
    now = int(time.time())

    def token(**claims: Any) -> str:
        body = {
            "iss": "https://securetoken.google.com/caselens-synthetic",
            "aud": "caselens-synthetic",
            "sub": "SYNTHETIC-anon-uid-1",
            "iat": now,
            "exp": now + 600,
            **claims,
        }
        return jwt.encode(body, key, algorithm="RS256")

    anonymous = verifier.verify(token(firebase={"sign_in_provider": "anonymous"}))
    assert anonymous.is_anonymous and anonymous.subject == "SYNTHETIC-anon-uid-1"
    assert anonymous.email is None
    password = verifier.verify(token(firebase={"sign_in_provider": "password"}))
    assert not password.is_anonymous and password.sign_in_provider == "password"
    assert verifier.verify(token()).sign_in_provider is None
