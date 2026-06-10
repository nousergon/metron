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
    # Personal/single-operator mode: enables the server-side SnapTrade sync, which uses
    # ONE operator SnapTrade connection (SNAPTRADE_* env) shared by the process. Safe only
    # on a single-tenant deploy — OFF by default so a multi-tenant deploy can never let one
    # tenant pull another's brokerage data. M2's per-user SnapTrade connection-portal flow
    # replaces this; it is not this endpoint.
    snaptrade_personal: bool = False
    # Optional institution allowlist for the personal SnapTrade sync (comma-separated, e.g.
    # "Fidelity"). When set, the sync imports ONLY accounts at those institutions — so a
    # SnapTrade connection that also exposes other brokers (e.g. IBKR) composes with a
    # better-quality Flex/native source for those without double-counting. Empty → all.
    snaptrade_institutions: str = ""

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def snaptrade_institution_list(self) -> list[str]:
        return [s.strip() for s in self.snaptrade_institutions.split(",") if s.strip()]


settings = Settings()
