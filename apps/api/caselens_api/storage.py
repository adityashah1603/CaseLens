"""Evidence object storage for the API, behind a thin interface.

The API needs three things the workers' `ObjectStore` does not: streaming uploads from a file
object, a streaming SHA-256 re-check, and short-lived signed GET URLs. `S3EvidenceStore` talks to
MinIO (local) or any S3 endpoint; `LocalDiskEvidenceStore` keeps objects on the local disk and
emulates signed URLs with expiring HMAC tokens (the free single-container demo, D-066);
`MemoryEvidenceStore` wraps the workers' `MemoryObjectStore` for tests. Object keys come from
`caselens_workers.storage.evidence_key`, so the workers read exactly what the API wrote.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import mimetypes
import os
import tempfile
import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import BinaryIO, Protocol
from urllib.parse import quote

from caselens_api.settings import Settings
from caselens_workers.storage import MemoryObjectStore

HASH_CHUNK = 1024 * 1024


class StorageError(RuntimeError):
    """The object store could not be reached or the object is missing."""


class EvidenceStore(Protocol):
    def put_stream(self, key: str, data: BinaryIO, size: int, content_type: str) -> str:
        """Store `size` bytes from `data` under `key`; return the object URI."""
        ...

    def sha256(self, uri: str) -> str:
        """Stream the object at `uri` and return its lowercase hex SHA-256."""
        ...

    def signed_url(self, uri: str, ttl_s: int) -> str:
        """A GET URL for the object that expires after `ttl_s` seconds."""
        ...


def split_uri(uri: str) -> tuple[str, str, str]:
    """'s3://bucket/key' -> ('s3', 'bucket', 'key')."""
    scheme, sep, rest = uri.partition("://")
    bucket, _, key = rest.partition("/")
    if not sep or not bucket or not key:
        raise StorageError("not an object URI")
    return scheme, bucket, key


class S3EvidenceStore:
    """MinIO / S3. The endpoint must be reachable by the browser for signed URLs
    (127.0.0.1:9000 in docker compose)."""

    def __init__(self, settings: Settings) -> None:
        from minio import Minio

        self.bucket = settings.bucket
        self._client = Minio(
            settings.s3_endpoint,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
            secure=settings.s3_secure,
            region=settings.s3_region,
        )

    def put_stream(self, key: str, data: BinaryIO, size: int, content_type: str) -> str:
        from minio.error import MinioException

        try:
            self._client.put_object(self.bucket, key, data, size, content_type=content_type)
        except (MinioException, OSError) as exc:
            raise StorageError("object store upload failed") from exc
        return f"s3://{self.bucket}/{key}"

    def sha256(self, uri: str) -> str:
        from minio.error import MinioException

        _, bucket, key = split_uri(uri)
        digest = hashlib.sha256()
        try:
            response = self._client.get_object(bucket, key)
            try:
                for chunk in response.stream(HASH_CHUNK):
                    digest.update(chunk)
            finally:
                response.close()
                response.release_conn()
        except (MinioException, OSError) as exc:
            raise StorageError("object store read failed") from exc
        return digest.hexdigest()

    def signed_url(self, uri: str, ttl_s: int) -> str:
        from minio.error import MinioException

        _, bucket, key = split_uri(uri)
        try:
            url: str = self._client.presigned_get_object(
                bucket, key, expires=timedelta(seconds=ttl_s)
            )
        except (MinioException, OSError) as exc:
            raise StorageError("could not sign the object URL") from exc
        return url


class MemoryEvidenceStore:
    """In-process store for tests (SYNTHETIC bytes only)."""

    def __init__(self, store: MemoryObjectStore | None = None) -> None:
        self.store = store or MemoryObjectStore()

    def put_stream(self, key: str, data: BinaryIO, size: int, content_type: str) -> str:
        return self.store.put_bytes(key, data.read(size), content_type)

    def sha256(self, uri: str) -> str:
        _, _, key = split_uri(uri)
        if key not in self.store.objects:
            raise StorageError("object is missing")
        return hashlib.sha256(self.store.objects[key]).hexdigest()

    def signed_url(self, uri: str, ttl_s: int) -> str:
        _, bucket, key = split_uri(uri)
        return f"https://storage.invalid/{quote(bucket)}/{quote(key)}?expires_in={ttl_s}"


# ---- local disk + HMAC-signed URLs ---------------------------------------------------------

STORAGE_ROUTE = "/storage"
META_SUFFIX = ".meta.json"
MAX_TOKEN_LENGTH = 2048


class InvalidStorageTokenError(StorageError):
    """The /storage token is malformed, not signed with our secret, or expired."""


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as exc:
        raise InvalidStorageTokenError("malformed token") from exc


def key_segments(key: str) -> list[str]:
    """Validate an object key as a relative path under the store root (no traversal, no
    drive letters, no sidecar names); return its segments."""
    parts = key.split("/")
    if (
        not key
        or "\\" in key
        or "\x00" in key
        or any(part in ("", ".", "..") or ":" in part for part in parts)
        or parts[-1].endswith(META_SUFFIX)
    ):
        raise StorageError("invalid object key")
    return parts


def sign_storage_token(secret: bytes, key: str, expires_at: int) -> str:
    """`base64url(expiry LF key) "." base64url(HMAC-SHA256(secret, expiry LF key))`."""
    key_segments(key)
    payload = f"{expires_at}\n{key}".encode()
    mac = hmac.new(secret, payload, hashlib.sha256).digest()
    return f"{_b64(payload)}.{_b64(mac)}"


def verify_storage_token(secret: bytes, token: str, now: float) -> str:
    """The object key of a valid, unexpired token; InvalidStorageTokenError otherwise. The MAC is
    checked (constant time) before anything in the payload is trusted."""
    if len(token) > MAX_TOKEN_LENGTH or token.count(".") != 1:
        raise InvalidStorageTokenError("malformed token")
    payload_b64, mac_b64 = token.split(".")
    payload, mac = _unb64(payload_b64), _unb64(mac_b64)
    expected = hmac.new(secret, payload, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected):
        raise InvalidStorageTokenError("bad signature")
    expiry_text, sep, key = payload.decode("utf-8", errors="replace").partition("\n")
    if not sep or not expiry_text.isdigit():
        raise InvalidStorageTokenError("malformed token")
    if now > int(expiry_text):
        raise InvalidStorageTokenError("expired token")
    try:
        key_segments(key)
    except StorageError as exc:
        raise InvalidStorageTokenError("invalid object key") from exc
    return key


class LocalDiskEvidenceStore:
    """Objects as files under `root`, by the same keys as the `s3://bucket/key` URIs in
    evidence.gcs_uri, with the content type in a `<file>.meta.json` sidecar. `signed_url`
    returns `<public_base_url>/storage/<token>`: an expiring HMAC-signed token that the
    /storage route (an object-store emulation, not part of the API contract) verifies before
    it serves the file."""

    def __init__(
        self,
        root: Path,
        bucket: str,
        secret: bytes,
        public_base_url: str = "",
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.root = root.resolve()
        self.bucket = bucket
        self._secret = secret
        self.public_base_url = public_base_url.rstrip("/")
        self._clock = clock

    @classmethod
    def from_settings(cls, settings: Settings) -> LocalDiskEvidenceStore:
        if settings.local_storage_root is None or settings.storage_signing_secret is None:
            raise StorageError("local storage needs a root and a signing secret")
        return cls(
            settings.local_storage_root,
            settings.bucket,
            settings.storage_signing_secret.get_secret_value().encode(),
            settings.public_base_url,
        )

    def path_for(self, key: str) -> Path:
        path = self.root.joinpath(*key_segments(key))
        if not path.resolve().is_relative_to(self.root):  # a symlink out of the root
            raise StorageError("invalid object key")
        return path

    def _key(self, uri: str) -> str:
        _, bucket, key = split_uri(uri)
        if bucket != self.bucket:
            raise StorageError("the object is not in this store's bucket")
        return key

    def put_stream(self, key: str, data: BinaryIO, size: int, content_type: str) -> str:
        path = self.path_for(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".upload-")
            try:
                with os.fdopen(fd, "wb") as out:
                    remaining = size
                    while remaining > 0:
                        chunk = data.read(min(HASH_CHUNK, remaining))
                        if not chunk:
                            raise StorageError("the upload ended early")
                        out.write(chunk)
                        remaining -= len(chunk)
                meta = path.with_name(path.name + META_SUFFIX)
                meta.write_text(json.dumps({"content_type": content_type}), encoding="utf-8")
                os.replace(tmp, path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        except OSError as exc:
            raise StorageError("object store upload failed") from exc
        return f"s3://{self.bucket}/{key}"

    def sha256(self, uri: str) -> str:
        digest = hashlib.sha256()
        try:
            with self.path_for(self._key(uri)).open("rb") as handle:
                for chunk in iter(lambda: handle.read(HASH_CHUNK), b""):
                    digest.update(chunk)
        except OSError as exc:
            raise StorageError("object store read failed") from exc
        return digest.hexdigest()

    def signed_url(self, uri: str, ttl_s: int) -> str:
        expires_at = int(self._clock()) + ttl_s
        token = sign_storage_token(self._secret, self._key(uri), expires_at)
        return f"{self.public_base_url}{STORAGE_ROUTE}/{token}"

    def resolve_token(self, token: str) -> tuple[Path, str]:
        """(file, content type) for a valid token. Raises InvalidStorageTokenError, or StorageError
        when the object is missing."""
        key = verify_storage_token(self._secret, token, self._clock())
        path = self.path_for(key)
        if not path.is_file():
            raise StorageError("object is missing")
        return path, self.content_type(path)

    @staticmethod
    def content_type(path: Path) -> str:
        meta = path.with_name(path.name + META_SUFFIX)
        try:
            value = json.loads(meta.read_text(encoding="utf-8")).get("content_type")
        except (OSError, ValueError, AttributeError):
            value = None
        if isinstance(value, str) and "/" in value and "\n" not in value and "\r" not in value:
            return value
        return mimetypes.guess_type(path.name)[0] or "application/octet-stream"
