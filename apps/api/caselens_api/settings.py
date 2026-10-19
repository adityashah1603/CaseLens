from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. Values come from CASELENS_* environment variables."""

    model_config = SettingsConfigDict(env_prefix="CASELENS_")

    environment: str = "local"
    database_url: str | None = None
