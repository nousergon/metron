"""Application settings, loaded from environment (.env in dev)."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Default: local Postgres (Docker). SQLite fallback for quick dev without Docker:
    #   DATABASE_URL=sqlite:///./dev.sqlite uvicorn api.main:app
    database_url: str = "postgresql+psycopg://postgres:metron@localhost:5432/metron"
    cors_origins: str = "http://localhost:3000"
    env: str = "dev"
    # Escape hatch for a deliberate non-dev SQLite deployment. Off by default so a
    # deployed process that has lost the environment naming its real database FAILS
    # instead of silently reading a stale local file (metron-ops#264; see
    # api.db.session._assert_database_is_deliberate).
    allow_sqlite: bool = False
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
    # How long a broker may go without a successful reconciliation fetch before the
    # nightly job exits non-zero and turns the systemd unit (and box-health) red
    # (metron-ops#274). Every fetch failure still alerts to Telegram immediately,
    # whatever this is set to — this governs ESCALATION, not visibility. 36h is one
    # nightly cadence plus headroom: a single missed run is a busy upstream, two in a
    # row is the detector being down, and only the second is worth a CRITICAL page.
    reconciliation_stale_hours: float = 36.0
    # Shadow-recompute divergence tolerances (metron-ops#218, dashboard-accuracy layer 3)
    # — NAV/realized-P&L allow a small absolute-or-relative band (broker-rounding /
    # as-of-price-staleness noise between the two independent valuation passes), same
    # shape as the reconciliation tolerances above. TWR is a % figure so its floor is a
    # flat bp band, not a $ one.
    shadow_recompute_nav_tolerance_usd: float = 1.0
    shadow_recompute_nav_tolerance_bps: float = 10.0
    shadow_recompute_twr_tolerance_bps: float = 25.0
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
    # External user demo (metron-ops-I310). An invited viewer redeems a single-use invite
    # code and gets a server-side session pinned to the no-advice feature set over the
    # demo household (see api/services/external_demo.py).
    #
    # RELEASE GATE: while False, invite creation AND redemption return 403 and every
    # existing external-demo session stops resolving. It flips only after the licensed
    # display entitlement is confirmed (metron-ops#24); nothing in this repo flips it.
    external_demo_released: bool = False
    # Fallback (metron-ops-I310 rescope item 5): True re-locks the feed-derived features
    # (risk, attribution, benchmark, scenarios, calendar, indices, ETF look-through) for
    # external-demo sessions and shows them as locked cards — a config change, no code change.
    external_demo_feed_features_locked: bool = False
    # Comma-separated verified identity emails allowed to create invites and read the
    # funnel counters. Empty (the default) means nobody: the owner endpoints fail closed.
    external_demo_admin_emails: str = ""
    # Invite and session lifetimes. A session is also dead the moment the release flag is off.
    external_demo_invite_ttl_hours: int = 14 * 24
    external_demo_session_ttl_hours: int = 72
    # v1-fed surfaces cutover (metron-ops-I308, Brian R4 2026-09-14): research intel,
    # factor attractiveness and the Alpha Engine overlay all read v1 crucible-research /
    # predictor artifacts that stop being written at crucible v2 phase 4. False today —
    # those surfaces instead render an artifact-age STALE state. Flipping this to True is
    # done ONLY by the v2 phase-4 cutover PR (never a routine config change); once on,
    # `api.entitlements.resolve` marks research_intel/alpha_engine `reason="retired"`
    # (terminal, not an upsell), which cascades to the nav, the routers and the insight
    # registry's candidate-facet filter without a second flag anywhere.
    retired_v1_surfaces: bool = False

    @property
    def external_demo_admin_email_set(self) -> frozenset[str]:
        return frozenset(e.strip().lower() for e in self.external_demo_admin_emails.split(",") if e.strip())

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
