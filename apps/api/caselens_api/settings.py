"""API configuration from CASELENS_* environment variables (see .env.example)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LOCAL_ENVIRONMENT = "local"
DEFAULT_ENVIRONMENT = "prod"
# Pool connections beyond the two each in-flight /ask holds (request + read-only SQL), for the
# other endpoints and SSE polls.
POOL_HEADROOM = 4
MIN_SIGNING_SECRET_LENGTH = 32


class Settings(BaseSettings):
    """Runtime configuration. Values come from CASELENS_* environment variables."""

    model_config = SettingsConfigDict(env_prefix="CASELENS_", extra="ignore")

    # Fail closed: anything but an explicit CASELENS_ENVIRONMENT=local is treated as deployed
    # (no dev tokens, no superuser / BYPASSRLS database login).
    environment: str = DEFAULT_ENVIRONMENT
    # Owner URL (alembic migrations). The API pool uses api_database_url when it is set.
    database_url: str | None = None
    # Dedicated API login (caselens_api_login locally): NOSUPERUSER, NOBYPASSRLS, member of the
    # app and read-only group roles only. Checked at startup (refused outside local otherwise).
    api_database_url: str | None = None
    # Group roles the pooled login switches to per request with SET ROLE; it never serves a
    # request as itself.
    app_db_role: str = "caselens_app"
    readonly_db_role: str = "caselens_readonly"
    db_pool_min_size: int = 1
    # None -> 2 * ask_max_concurrent + POOL_HEADROOM (see pool_max_size).
    db_pool_max_size: int | None = None

    # Auth. Firebase ID tokens in every environment that has a project id; the HS256 dev
    # verifier only when environment == "local" (refused at startup otherwise).
    firebase_project_id: str | None = None
    dev_auth_secret: SecretStr | None = None
    # Public demo (D-066): a verified Firebase *anonymous* token is served as the shared demo
    # viewer user (oidc_subject "demo:viewer"). Off -> anonymous tokens are rejected with 401.
    anonymous_demo: bool = False

    # Object storage. "s3": MinIO / S3 (same variables as the workers' WorkerSettings).
    # "local": files under local_storage_root by object key, served by GET /storage/<token>
    # with HMAC-signed, expiring tokens (the free single-container demo, D-066).
    storage_backend: Literal["s3", "local"] = "s3"
    local_storage_root: Path | None = None
    storage_signing_secret: SecretStr | None = None
    # Prefix of the signed /storage URLs ("" = same-origin relative URLs).
    public_base_url: str = ""
    s3_endpoint: str = "127.0.0.1:9000"
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    s3_secure: bool = False
    s3_region: str = "us-east-1"
    bucket: str = "caselens-evidence"
    signed_url_ttl_s: int = 300
    max_upload_bytes: int = 500 * 1024 * 1024

    # Built web app (apps/web/dist) served at / with index.html fallback; unset = API only.
    web_dist: Path | None = None

    # Browser origins allowed by CORS (the Vite dev server by default).
    cors_origins: list[str] = ["http://127.0.0.1:5173", "http://localhost:5173"]

    # /ask rate limit: at most `ask_rate_limit` questions per user per `ask_rate_window_s`,
    # counted in this process (one uvicorn worker locally; Cloud Armor at the edge in prod).
    ask_rate_limit: int = 6
    ask_rate_window_s: int = 60
    # Concurrent /ask runs in this process (each holds two pool connections for the whole run);
    # over the limit -> 429 with Retry-After.
    ask_max_concurrent: int = 2
    ask_busy_retry_after_s: int = 5
    # Demo users share one user id, so /ask limits them per client IP. 0 = use the socket peer
    # address; N > 0 = trust N proxies in front of the API and take the N-th X-Forwarded-For
    # entry from the right (the address the outermost trusted proxy saw).
    trusted_proxy_hops: int = 0
    # Load the query embedder + reranker at startup (~7 s once) instead of on the first question.
    # Tests with fake agent backends turn it off.
    warm_up_models: bool = True

    # SSE: poll interval for ingest_jobs, heartbeat interval, and max stream lifetime.
    sse_poll_s: float = 1.5
    sse_heartbeat_s: float = 15.0
    sse_max_stream_s: float = 1800.0

    @model_validator(mode="after")
    def _check_pool(self) -> Settings:
        if self.ask_max_concurrent < 1:
            raise ValueError("CASELENS_ASK_MAX_CONCURRENT must be at least 1")
        minimum = 2 * self.ask_max_concurrent + 1
        if self.db_pool_max_size is not None and self.db_pool_max_size < minimum:
            raise ValueError(
                f"CASELENS_DB_POOL_MAX_SIZE must be at least {minimum} "
                "(2 * CASELENS_ASK_MAX_CONCURRENT + 1)"
            )
        if self.trusted_proxy_hops < 0:
            raise ValueError("CASELENS_TRUSTED_PROXY_HOPS must not be negative")
        if self.storage_backend == "local":
            if self.local_storage_root is None:
                raise ValueError(
                    "CASELENS_LOCAL_STORAGE_ROOT is required with storage_backend=local"
                )
            secret = self.storage_signing_secret
            if secret is None or len(secret.get_secret_value()) < MIN_SIGNING_SECRET_LENGTH:
                raise ValueError(
                    "CASELENS_STORAGE_SIGNING_SECRET must be at least "
                    f"{MIN_SIGNING_SECRET_LENGTH} characters with storage_backend=local"
                )
        return self

    @property
    def is_local(self) -> bool:
        return self.environment == LOCAL_ENVIRONMENT

    @property
    def pool_url(self) -> str | None:
        """The URL the API pool logs in with: the dedicated API login when configured."""
        return self.api_database_url or self.database_url

    @property
    def pool_max_size(self) -> int:
        if self.db_pool_max_size is not None:
            return self.db_pool_max_size
        return 2 * self.ask_max_concurrent + POOL_HEADROOM


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
