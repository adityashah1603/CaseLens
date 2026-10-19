"""Object storage behind a thin interface: MinIO (S3 API) locally, GCS in prod."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Protocol

from caselens_workers.settings import WorkerSettings


class ObjectStore(Protocol):
    scheme: str
    bucket: str

    def put_file(self, key: str, path: Path, content_type: str) -> str: ...
    def put_bytes(self, key: str, data: bytes, content_type: str) -> str: ...
    def get_bytes(self, key: str) -> bytes: ...
    def download(self, key: str, path: Path) -> None: ...
    def exists(self, key: str) -> bool: ...


def evidence_key(case_id: object, evidence_id: object) -> str:
    return f"cases/{case_id}/evidence/{evidence_id}/original"


def artifact_key(case_id: object, evidence_id: object, stage: str, name: str) -> str:
    """Deterministic key for a stage's derived artifact, so a retry overwrites, never duplicates."""
    return f"cases/{case_id}/evidence/{evidence_id}/{stage}/{name}"


def split_uri(uri: str) -> tuple[str, str]:
    """'s3://bucket/key' or 'gs://bucket/key' -> (bucket, key)."""
    _, _, rest = uri.partition("://")
    bucket, _, key = rest.partition("/")
    if not bucket or not key:
        raise ValueError(f"not an object URI: {uri!r}")
    return bucket, key


class S3ObjectStore:
    """MinIO / any S3-compatible endpoint (local docker compose)."""

    scheme = "s3"

    def __init__(self, settings: WorkerSettings) -> None:
        from minio import Minio

        self.bucket = settings.bucket
        self._client = Minio(
            settings.s3_endpoint,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
            secure=settings.s3_secure,
        )

    def uri(self, key: str) -> str:
        return f"{self.scheme}://{self.bucket}/{key}"

    def put_file(self, key: str, path: Path, content_type: str) -> str:
        self._client.fput_object(self.bucket, key, str(path), content_type=content_type)
        return self.uri(key)

    def put_bytes(self, key: str, data: bytes, content_type: str) -> str:
        self._client.put_object(
            self.bucket, key, io.BytesIO(data), len(data), content_type=content_type
        )
        return self.uri(key)

    def get_bytes(self, key: str) -> bytes:
        response = self._client.get_object(self.bucket, key)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    def download(self, key: str, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._client.fget_object(self.bucket, key, str(path))

    def exists(self, key: str) -> bool:
        from minio.error import S3Error

        try:
            self._client.stat_object(self.bucket, key)
        except S3Error as exc:
            if exc.code in ("NoSuchKey", "NoSuchObject"):
                return False
            raise
        return True


class MemoryObjectStore:
    """In-process store for tests."""

    scheme = "mem"

    def __init__(self, bucket: str = "test") -> None:
        self.bucket = bucket
        self.objects: dict[str, bytes] = {}

    def put_file(self, key: str, path: Path, content_type: str) -> str:
        return self.put_bytes(key, path.read_bytes(), content_type)

    def put_bytes(self, key: str, data: bytes, content_type: str) -> str:
        self.objects[key] = data
        return f"{self.scheme}://{self.bucket}/{key}"

    def get_bytes(self, key: str) -> bytes:
        return self.objects[key]

    def download(self, key: str, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.objects[key])

    def exists(self, key: str) -> bool:
        return key in self.objects


def object_store(settings: WorkerSettings) -> ObjectStore:
    return S3ObjectStore(settings)
