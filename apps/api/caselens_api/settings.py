"""API configuration from CASELENS_* environment variables (see .env.example)."""

from __future__ import annotations

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

LOCAL_ENVIRONMENT = "local"


class Settings(BaseSettings):
    """Runtime configuration. Values come from CASELENS_* environment variables."""

    model_config = SettingsConfigDict(env_prefix="CASELENS_", extra="ignore")

    environment: str = LOCAL_ENVIRONMENT
    database_url: str | None = None
    # Group roles the pooled login switches to per request. In docker compose the login user is
    # the owner, so it drops to these with SET ROLE; it never serves a request as itself.
    app_db_role: str = "caselens_app"
    readonly_db_role: str = "caselens_readonly"
    db_pool_min_size: int = 1
    db_pool_max_size: int = 8

    # Auth. Firebase ID tokens in every environment that has a project id; the HS256 dev
    # verifier only when environment == "local" (refused at startup otherwise).
    firebase_project_id: str | None = None
    dev_auth_secret: SecretStr | None = None

    # Object storage (same variables as the workers' WorkerSettings).
    s3_endpoint: str = "127.0.0.1:9000"
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    s3_secure: bool = False
    s3_region: str = "us-east-1"
    bucket: str = "caselens-evidence"
    signed_url_ttl_s: int = 300
    max_upload_bytes: int = 500 * 1024 * 1024

    # Browser origins allowed by CORS (the Vite dev server by default).
    cors_origins: list[str] = ["http://127.0.0.1:5173", "http://localhost:5173"]

    # /ask rate limit: at most `ask_rate_limit` questions per user per `ask_rate_window_s`,
    # counted in this process (one uvicorn worker locally; Cloud Armor at the edge in prod).
    ask_rate_limit: int = 6
    ask_rate_window_s: int = 60

    # SSE: poll interval for ingest_jobs, heartbeat interval, and max stream lifetime.
    sse_poll_s: float = 1.5
    sse_heartbeat_s: float = 15.0
    sse_max_stream_s: float = 1800.0

    @property
    def is_local(self) -> bool:
        return self.environment == LOCAL_ENVIRONMENT


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
