from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    """Worker configuration from CASELENS_* environment variables (see .env.example)."""

    model_config = SettingsConfigDict(env_prefix="CASELENS_", extra="ignore")

    database_url: str | None = None
    # Role the worker connection switches to. In compose the login user is the owner, so the
    # worker drops to caselens_worker; in prod the login user is already a member of it.
    worker_db_role: str | None = "caselens_worker"
    # Role used by the dev seeding CLI, which acts as a case lead through RLS like the API does.
    app_db_role: str | None = "caselens_app"

    s3_endpoint: str = "127.0.0.1:9000"
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    s3_secure: bool = False
    bucket: str = "caselens-evidence"

    model_cache_dir: Path = Path(".cache/models")
    embedding_model: str = "BAAI/bge-base-en-v1.5"
    # Threads per heavy model (ONNX / torch). Kept low so a laptop stays responsive.
    model_threads: int = 2
    max_attempts: int = 3
    stale_running_after_s: int = 1800


def libpq_url(url: str) -> str:
    """Accept SQLAlchemy-style URLs (postgresql+psycopg://) as plain libpq ones."""
    scheme, sep, rest = url.partition("://")
    return f"{scheme.split('+', 1)[0]}{sep}{rest}" if sep else url
