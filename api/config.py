"""Application settings, loaded from environment (.env in dev)."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Dev default: local SQLite, no vendor cost. Prod: postgresql+psycopg://…
    database_url: str = "sqlite:///./dev.sqlite"
    cors_origins: str = "http://localhost:3000"
    env: str = "dev"
    # Shared identity service (metron-ops#179) — nousergon-auth. Metron no longer runs
    # its own Better Auth instance; the shared service at `auth_base_url` authenticates
    # users and mints short-lived EdDSA JWTs, which the API verifies locally against the
    # service's JWKS (see api.services.auth_jwt — no per-request round-trip). `iss` and
    # `aud` both default to the service's base URL; `auth_jwt_audience` is an override in
    # case the deployed service ever pins a custom audience.
    auth_base_url: str = "https://auth.nousergon.ai"
    auth_jwt_audience: str | None = None
    auth_jwks_cache_seconds: int = 300
    # Personal/single-operator mode: enables the server-side SnapTrade sync, which uses
    # ONE operator SnapTrade connection (SNAPTRADE_* env) shared by the process. Safe only
    # on a single-tenant deploy — OFF by default so a multi-tenant deploy can never let one
    # tenant pull another's brokerage data. M2's per-user SnapTrade connection-portal flow
    # replaces this; it is not this endpoint.
    snaptrade_personal: bool = False
    # Stored IBKR Flex credentials (single-operator owner build) — when both are set, the
    # IBKR sync runs from these instead of a per-request token paste (metron-ops#82), the
    # same server-side-credential pattern as SnapTrade above. Treated as a secret (the Flex
    # token is read-only + expirable). Empty → the UI falls back to the BYO-token form.
    flex_token: str = ""
    flex_query_id: str = ""
    # Custodian-reconciliation alerting (metron-ops#216) — a Telegram bot/chat the
    # nightly reconciliation job posts break summaries to. Hydrated from SSM
    # (/metron/telegram_bot_token, /metron/telegram_chat_id) same as the credentials
    # above; empty means unconfigured, in which case the job logs (routed through
    # flow-doctor) instead of posting, rather than failing the run.
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    # Reconciliation break tolerances (metron-ops#216) — quantity is exact-match
    # (a share-count mismatch is never "rounding"); cost-basis/cash allow a small
    # absolute-or-relative band for FX conversion + broker rounding, whichever is
    # larger, so a $50k position isn't flagged for a one-cent FX rounding diff and a
    # $10 position isn't flagged for its own cost basis being $0.02 off.
    reconciliation_cash_tolerance_usd: float = 1.0
    reconciliation_cost_basis_tolerance_bps: float = 5.0
    # Data-spine sync (metron ↔ alpha-engine-data). `alpha-engine-data` is the system's
    # sole market-data producer; Metron publishes its held-ticker universe here and reads
    # back EOD-close / FX artifacts (no direct market-data API calls). The bucket is the
    # shared alpha-engine S3 store. OFF by default so dev/tests never reach S3; the prod
    # deploy sets MARKET_DATA_SYNC_ENABLED=true (instance role grants the bucket). This is
    # an INFRA toggle ONLY — it does NOT gate the entitlement feed axis (see feed_entitled).
    market_data_bucket: str = "alpha-engine-research"
    market_data_sync_enabled: bool = False
    # Feed entitlement (entitlement axis 2): does this deployment OFFER the feed-dependent
    # wedge (risk / attribution / scenarios / benchmark)? DECOUPLED from
    # market_data_sync_enabled (the S3 data-spine infra toggle above) so the owner build
    # never self-gates its own analytics — risk/attribution compute factor history via
    # on-demand price backfill, independent of the S3 spine. Default True (the personal/
    # owner build is fully provisioned). The public multi-tenant BETA deploy sets
    # FEED_ENTITLED=false (+ DEFAULT_TIER=beta) so the no-feed beta shows only the
    # free-derivable set. See metron-ops#43 (the conflation that emptied the owner risk page).
    feed_entitled: bool = True
    # Product tier this deployment serves. The personal build runs the full
    # "personal" superset; real per-tenant subscription gating supersedes this in M2.
    default_tier: str = "personal"
    # Demo/sample portfolio (metron-ops#42) — seed a canned, frozen, READ-ONLY fixture
    # at startup so a prospect can explore the product with no signup/connection. On by
    # default; a deploy that doesn't want the sample portfolio sets DEMO_ENABLED=false.
    demo_enabled: bool = True
    # Tier simulator — owner-only preview of Beta / Pro / Research+ / Base product
    # levels in the personal build, via GET /meta/entitlements?preview_tier=&preview_feed=.
    # NEVER enabled on the public multi-tenant product (it would let any caller
    # re-scope their own entitlements). See metron-ops#37.
    tier_simulator: bool = False

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
