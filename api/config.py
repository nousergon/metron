"""Application settings, loaded from environment (.env in dev)."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Dev default: local SQLite, no vendor cost. Prod: postgresql+psycopg://…
    database_url: str = "sqlite:///./dev.sqlite"
    cors_origins: str = "http://localhost:3000"
    env: str = "dev"
    # Personal/single-operator mode: enables the server-side SnapTrade sync, which uses
    # ONE operator SnapTrade connection (SNAPTRADE_* env) shared by the process. Safe only
    # on a single-tenant deploy — OFF by default so a multi-tenant deploy can never let one
    # tenant pull another's brokerage data. M2's per-user SnapTrade connection-portal flow
    # replaces this; it is not this endpoint.
    snaptrade_personal: bool = False
    # Data-spine sync (metron ↔ alpha-engine-data). `alpha-engine-data` is the system's
    # sole market-data producer; Metron publishes its held-ticker universe here and reads
    # back EOD-close / FX artifacts (no direct market-data API calls). The bucket is the
    # shared alpha-engine S3 store. OFF by default so dev/tests never reach S3; the prod
    # deploy sets MARKET_DATA_SYNC_ENABLED=true (instance role grants the bucket).
    market_data_bucket: str = "alpha-engine-research"
    market_data_sync_enabled: bool = False
    # Product tier this deployment serves. The personal build runs the full
    # "personal" superset; real per-tenant subscription gating supersedes this in M2.
    default_tier: str = "personal"
    # Tier simulator — owner-only preview of Beta / Pro / Research+ / Base product
    # levels in the personal build, via GET /meta/entitlements?preview_tier=&preview_feed=.
    # NEVER enabled on the public multi-tenant product (it would let any caller
    # re-scope their own entitlements). See metron-ops#37.
    tier_simulator: bool = False

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
