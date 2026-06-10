"""Application settings, loaded from environment (.env in dev)."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Dev default: local SQLite, no vendor cost. Prod: postgresql+psycopg://…
    database_url: str = "sqlite:///./dev.sqlite"
    cors_origins: str = "http://localhost:3000"
    env: str = "dev"
    # FRED API key for the Macro page (free, https://fred.stlouisfed.org/docs/api/api_key.html).
    # Unset → the Macro page reports "not configured" rather than fabricating data.
    fred_api_key: str | None = None

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
