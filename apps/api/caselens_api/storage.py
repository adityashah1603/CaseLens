"""Evidence object storage for the API, behind a thin interface.

The API needs three things the workers' `ObjectStore` does not: streaming uploads from a file
object, a streaming SHA-256 re-check, and short-lived signed GET URLs. `S3EvidenceStore` talks to
MinIO (local) or any S3 endpoint; `MemoryEvidenceStore` wraps the workers' `MemoryObjectStore`
for tests. Object keys come from `caselens_workers.storage.evidence_key`, so the workers read
exactly what the API wrote.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
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
