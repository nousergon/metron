"""Showcase Portfolio — the single read-only illustrative showcase portfolio, open to
anyone with no signup and no brokerage connection (metron-ops#42), and also visible on
every real tenant's own dashboard (api/routers/portfolios.py::list_portfolios).

Renamed from "Reference Rate" — that term collides with the established, unrelated
finance meaning (a benchmark interest rate — SOFR, Fed Funds, Prime), confusing for the
financially-literate audience this is aimed at. Internal identifiers (module/function
names, ``REFERENCE_PORTFOLIO_ID``, the ``"reference"``/``"reference_sample"`` connector-
source labels) are unchanged — only the user-facing display name and disclaimer copy
moved. ``_ensure_reference_display_name_current`` self-heals the rename onto an
already-deployed instance (``Portfolio.name`` and the live sleeve's ``Account.name`` are
both write-once-at-creation elsewhere, so a bare constant rename alone would silently do
nothing there).

Two sleeves live under one portfolio id, seeded idempotently at startup:

- A LIVE sleeve synced daily from the engine's published artifact
  (``metron/reference_rate.json``) — real, moving, the actual product signal.
- A permanently-frozen SAMPLE sleeve (two accounts, three NON-EQUITY asset classes,
  plus dividends), replayed once through the same CSV import bridge a real upload
  uses, so the showcase also demonstrates account/asset-class breadth (tax-status
  grouping #46, security-type grouping #47) that the live sleeve alone (equity/ETF
  only, single account) can't exercise. Deliberately carries NO individual-stock
  equity positions of its own (AAPL/MSFT retired) — the Showcase Portfolio's equity
  count must equal Crucible's live holdings exactly; the sample sleeve may only add
  non-equity instruments (ETF/bond/cash), never additional equities.

The daily live sync only ever touches accounts present in ITS OWN artifact snapshot
(persistence._upsert_accounts matches by (tenant_id, broker, external_id)), so it can
never see or clobber the sample sleeve's accounts. ``NavSnapshot`` (the portfolio-level
TOTAL the Performance chart reads) folds in the sample sleeve's own constant total
value/cost-basis (``_sample_sleeve_totals``, read back from what was actually persisted
— never a separately-hardcoded duplicate that could drift) so the persisted history
stays consistent with what Holdings actually displays.

Formerly two separate portfolios (a static "Demo portfolio" for the no-auth entry, and
this live showcase for every tenant) — merged into one to cut showcase-portfolio
clutter; ``_retire_legacy_demo_portfolio`` cleans up the old one on any already-deployed
instance. Writes to the demo tenant are refused (``assert_writable``) so neither sleeve
can ever be mutated by a visitor.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from api.db import models
from api.services import analytics, persistence
from portfolio_analytics.broker_io.csv_import import parse_transactions_csv
from portfolio_analytics.ingestion import reference_connector

# Fixed, well-known ids (stable across restarts so links don't break). The tenant id
# spells "demo" in its tail; never issued to a real user (auth mints random UUIDs).
DEMO_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-00000000de60")

# The retired standalone "Demo portfolio" (metron-ops#42) this module used to seed at
# this id — superseded by folding its sample sleeve into REFERENCE_PORTFOLIO_ID below.
# Kept only so ``_retire_legacy_demo_portfolio`` can find and delete the leftover row on
# an already-deployed instance; nothing seeds this id anymore.
_LEGACY_DEMO_PORTFOLIO_ID = uuid.UUID("00000000-0000-0000-0000-00000000de61")

# The single showcase portfolio, seeded under the demo tenant but VISIBLE ON EVERY
# TENANT'S dashboard (api/routers/portfolios.py::list_portfolios + _owned_portfolio carve
# a narrow, named exception for this one fixed id): the "Showcase Portfolio". It carries
# no claims and states no objective; it is demo/illustrative only, meant to let a
# prospect explore real product behavior before linking their own accounts. Read-only
# for every tenant (not just the demo tenant) is enforced by the HTTP
# ``_demo_read_only`` middleware's path-based check in api/main.py — a fixed constant,
# so a real tenant's own portfolio can never collide with this id (``create_portfolio``
# always assigns a random uuid4, never a client-supplied id). The daily sync runs
# in-process (``api.maintenance`` / startup), bypassing the HTTP layer. The id and every
# internal identifier keep the pre-rename "reference" name — only the DISPLAY name below
# changed.
REFERENCE_PORTFOLIO_ID = uuid.UUID("00000000-0000-0000-0000-00000000de62")
REFERENCE_PORTFOLIO_NAME = "Showcase Portfolio"

# The frozen sample sleeve's distinct connector-source label — lets every query below
# scope precisely to "the fixture's own accounts" without ever touching the live sleeve's
# accounts (a different source, "reference") even though both now share one portfolio id.
_SAMPLE_SLEEVE_SOURCE = "reference_sample"

# Frozen transactions — two accounts, three non-equity asset classes (ETF/bond/cash)
# and dividends — the breadth the live equity/ETF-only sleeve can't demonstrate alone.
# Deliberately no individual-stock equity rows: the sample sleeve must never add
# equities beyond what Crucible's live sleeve actually holds (AAPL/MSFT retired).
_SAMPLE_SLEEVE_CSV = """date,type,symbol,quantity,price,amount,account
2024-01-08,BUY,VOO,15,440,6600,Sample Brokerage
2024-02-02,BUY,912828YK0,50,98,4900,Sample IRA
2024-03-15,BUY,VMFXX,2000,1,2000,Sample Brokerage
2024-06-03,DIVIDEND,VOO,0,0,38,Sample Brokerage
"""

# Per-symbol reference metadata applied after import (the CSV path defaults everything to
# equity): name + the asset_class that drives security-type grouping (#47).
_SAMPLE_SLEEVE_SECURITY_META: dict[str, tuple[str, str]] = {
    "VOO": ("Vanguard S&P 500 ETF", "etf"),
    "912828YK0": ("US Treasury Note 4.0% 2026", "bond"),
    "VMFXX": ("Vanguard Federal Money Market", "cash"),
}

# Frozen EOD closes (as-of date) so holdings value without a live price refresh. Also the
# SOLE source for the sleeve's contribution to NavSnapshot (see _sample_sleeve_totals) —
# never refreshed, so that contribution is provably constant, never a second duplicate
# number that could drift from it.
_SAMPLE_SLEEVE_PRICE_AS_OF = date(2024, 6, 28)
_SAMPLE_SLEEVE_PRICES: dict[str, float] = {
    "VOO": 490.0,
    "912828YK0": 99.0,
    "VMFXX": 1.0,
}

# Public — the sleeve's held tickers, so a cross-cutting consumer (api/maintenance.py's
# daily price-refresh loop) can exclude them from a LIVE fetch without importing the
# private price table above (which exists only for this module's own seeding/totals math).
SAMPLE_SLEEVE_TICKERS = frozenset(_SAMPLE_SLEEVE_PRICES)

# Account tax treatment — the IRA is tax-deferred so the tax-status grouping (#46) shows
# both a taxable and a tax-advantaged bucket. (Sample Brokerage derives to Taxable.)
_SAMPLE_SLEEVE_ACCOUNT_TAX: dict[str, tuple[str, str | None]] = {
    # external_id -> (tax_treatment, account_type)
    "Sample IRA": ("tax_deferred", "IRA"),
}


def is_demo_tenant(tenant_id: uuid.UUID) -> bool:
    return tenant_id == DEMO_TENANT_ID


def assert_writable(tenant_id: uuid.UUID) -> None:
    """Refuse any write to the demo tenant — the sample portfolio is read-only so a
    visitor can never mutate the shared fixture. Imports, edits, deletes, price refresh
    and reconstruction all flow through this guard. Raises 403 for the demo tenant."""
    if is_demo_tenant(tenant_id):
        # Local import to avoid a module cycle (routers import this module).
        from fastapi import HTTPException

        raise HTTPException(status_code=403, detail="The demo portfolio is read-only.")


def _ensure_sample_sleeve_seeded(session: Session) -> None:
    """Fold the frozen breadth fixture into the Showcase Portfolio, once. Idempotent on
    the sleeve's own source label — independent of the portfolio-exists check in
    ``ensure_reference_seeded`` — so an already-deployed instance (seeded before this
    sleeve existed) self-heals with it on next startup."""
    already = session.scalars(
        select(models.Account.id).where(
            models.Account.portfolio_id == REFERENCE_PORTFOLIO_ID,
            models.Account.broker == _SAMPLE_SLEEVE_SOURCE,
        )
    ).first()
    if already is not None:
        return

    # Replay the frozen CSV through the real import bridge (securities + accounts + ledger).
    result = parse_transactions_csv(_SAMPLE_SLEEVE_CSV, source=_SAMPLE_SLEEVE_SOURCE)
    persistence.persist_snapshot(
        session, tenant_id=DEMO_TENANT_ID, portfolio_id=REFERENCE_PORTFOLIO_ID, snapshot=result.snapshot
    )
    _apply_sample_sleeve_security_meta(session)
    _apply_sample_sleeve_account_tax(session)
    _seed_sample_sleeve_prices(session)
    session.commit()


def _apply_sample_sleeve_security_meta(session: Session) -> None:
    """Set name + asset_class on the sample sleeve's securities (the CSV path leaves
    them equity)."""
    rows = session.scalars(
        select(models.Security).where(models.Security.symbol.in_(list(_SAMPLE_SLEEVE_SECURITY_META)))
    ).all()
    for sec in rows:
        meta = _SAMPLE_SLEEVE_SECURITY_META.get(sec.symbol)
        if meta:
            sec.name, sec.asset_class = meta


def _apply_sample_sleeve_account_tax(session: Session) -> None:
    rows = session.scalars(
        select(models.Account).where(
            models.Account.portfolio_id == REFERENCE_PORTFOLIO_ID,
            models.Account.broker == _SAMPLE_SLEEVE_SOURCE,
        )
    ).all()
    for acct in rows:
        tax = _SAMPLE_SLEEVE_ACCOUNT_TAX.get(acct.external_id)
        if tax:
            acct.tax_treatment, acct.account_type = tax


def _seed_sample_sleeve_prices(session: Session) -> None:
    """Frozen EOD close per held symbol so holdings show a market value (no live fetch)."""
    secs = session.scalars(
        select(models.Security).where(models.Security.symbol.in_(list(_SAMPLE_SLEEVE_PRICES)))
    ).all()
    for sec in secs:
        close = _SAMPLE_SLEEVE_PRICES.get(sec.symbol)
        if close is None:
            continue
        session.add(
            models.PriceBar(
                security_id=sec.id,
                bar_date=_SAMPLE_SLEEVE_PRICE_AS_OF,
                close=close,
                currency=sec.currency or "USD",
            )
        )


def live_sleeve_tickers(session: Session) -> frozenset[str]:
    """The live Crucible-synced sleeve's own held tickers. Used by
    ``api/maintenance.py``'s daily price-refresh loop so a ticker the live sleeve
    happens to ALSO hold (Crucible's universe can rotate into AAPL/MSFT/etc) is never
    skipped just because it collides with ``SAMPLE_SLEEVE_TICKERS``."""
    rows = session.execute(
        select(models.Security.symbol)
        .join(models.Position, models.Position.security_id == models.Security.id)
        .join(models.Account, models.Position.account_id == models.Account.id)
        .where(
            models.Account.portfolio_id == REFERENCE_PORTFOLIO_ID,
            models.Account.broker != _SAMPLE_SLEEVE_SOURCE,
        )
    ).all()
    return frozenset(symbol for (symbol,) in rows)


def _sample_sleeve_totals(session: Session) -> tuple[float, float]:
    """``(market_value, cost_basis)`` of the frozen sample sleeve, read back from its
    own already-persisted state × its own frozen EOD closes — never a separately
    hardcoded duplicate, so it can never drift from the seeded fixture. Folded into
    ``NavSnapshot`` by ``_seed_reference_nav`` so the portfolio-level total stays
    consistent with what Holdings actually displays across both sleeves.

    The sleeve is CSV/ledger-sourced (``persist_snapshot`` never writes ``Position``
    rows for a CSV import — "positions are ledger-derived at read time"), so current
    quantity/cost-basis has to come from ``analytics.holdings`` (the same
    ledger-replay every other read path uses), not a raw ``Position`` query."""
    account_ids = list(
        session.scalars(
            select(models.Account.id).where(
                models.Account.portfolio_id == REFERENCE_PORTFOLIO_ID,
                models.Account.broker == _SAMPLE_SLEEVE_SOURCE,
            )
        ).all()
    )
    if not account_ids:
        return 0.0, 0.0
    held = analytics.holdings(session, DEMO_TENANT_ID, REFERENCE_PORTFOLIO_ID, account_ids=account_ids)
    market_value = sum(h.quantity * _SAMPLE_SLEEVE_PRICES.get(h.ticker, 0.0) for h in held)
    cost_basis = sum(h.cost_basis for h in held)
    return market_value, cost_basis


def _retire_legacy_demo_portfolio(session: Session) -> None:
    """One-time cleanup: delete the old, now-unreachable standalone "Demo portfolio"
    (``_LEGACY_DEMO_PORTFOLIO_ID``) left behind by the merge into the Showcase
    Portfolio — the web ``/demo`` redirect no longer points at it, so on an
    already-deployed instance it would otherwise sit forever as orphaned dead data.
    Idempotent no-op once gone. ``AccountNavSnapshot``/``OpenLot``/``RealizedLot`` have
    no ORM cascade from ``Account`` (matches the same explicit-delete pattern used by
    the account-delete endpoint), so they're deleted explicitly; ``Account``'s own
    transactions/positions cascade via its ORM relationship when the portfolio is
    deleted."""
    portfolio = session.get(models.Portfolio, _LEGACY_DEMO_PORTFOLIO_ID)
    if portfolio is None:
        return
    account_ids = list(
        session.scalars(
            select(models.Account.id).where(models.Account.portfolio_id == _LEGACY_DEMO_PORTFOLIO_ID)
        ).all()
    )
    if account_ids:
        session.execute(
            delete(models.AccountNavSnapshot).where(models.AccountNavSnapshot.account_id.in_(account_ids))
        )
        session.execute(delete(models.OpenLot).where(models.OpenLot.account_id.in_(account_ids)))
        session.execute(delete(models.RealizedLot).where(models.RealizedLot.account_id.in_(account_ids)))
    session.execute(delete(models.NavSnapshot).where(models.NavSnapshot.portfolio_id == _LEGACY_DEMO_PORTFOLIO_ID))
    session.execute(
        delete(models.InvestorPreferences).where(
            models.InvestorPreferences.portfolio_id == _LEGACY_DEMO_PORTFOLIO_ID
        )
    )
    session.delete(portfolio)  # cascades Account -> transactions/positions (ORM relationship)
    session.commit()


# ── Showcase Portfolio (live illustrative showcase) ──────────────────────────


def ensure_reference_seeded(session: Session) -> bool:
    """Idempotently create the Showcase Portfolio shell under the demo tenant.

    Returns True if it created the portfolio this call, False if it already existed.
    Only creates the (possibly empty) portfolio so its link resolves immediately;
    ``sync_reference_holdings`` populates the live sleeve from the artifact (at startup +
    daily). Safe to call on every startup. The sample sleeve, intraday-on preference,
    display-name, and legacy-portfolio cleanup below all self-heal unconditionally on
    every call, independent of the create/exists branch, so an already-deployed
    instance catches up on next startup with no manual migration."""
    created = False
    if session.get(models.Portfolio, REFERENCE_PORTFOLIO_ID) is None:
        if session.get(models.Tenant, DEMO_TENANT_ID) is None:
            session.add(models.Tenant(id=DEMO_TENANT_ID, name="Demo"))
        session.add(
            models.Portfolio(
                id=REFERENCE_PORTFOLIO_ID,
                tenant_id=DEMO_TENANT_ID,
                name=REFERENCE_PORTFOLIO_NAME,
                base_currency="USD",
            )
        )
        session.commit()
        created = True
    _ensure_reference_intraday_default_on(session)
    _ensure_reference_display_name_current(session)
    _ensure_sample_sleeve_seeded(session)
    _retire_legacy_demo_portfolio(session)
    return created


def _ensure_reference_intraday_default_on(session: Session) -> None:
    """Force ``InvestorPreferences.intraday_enabled`` True for the Showcase Portfolio.

    The live overlay (``api/services/intraday.py::for_portfolio``) is normally gated
    by a per-portfolio user toggle, default OFF, settable only via
    ``PUT /portfolios/{id}/preferences``. But ``_demo_read_only`` (api/main.py) 403s
    every non-GET request against this portfolio's id — including that PUT — so a
    real user can never reach the toggle. Left alone, the showcase would sit
    permanently EOD-only during live market hours, which is not the intent (the
    EOD-sole-sourcing decision in ``_seed_reference_nav`` is about the persisted NAV
    history, not this overlay). System-seed it on instead, unconditionally, so a
    deployment seeded before this existed self-heals on next startup."""
    pref = session.scalars(
        select(models.InvestorPreferences).where(
            models.InvestorPreferences.tenant_id == DEMO_TENANT_ID,
            models.InvestorPreferences.portfolio_id == REFERENCE_PORTFOLIO_ID,
        )
    ).first()
    if pref is None:
        session.add(
            models.InvestorPreferences(
                tenant_id=DEMO_TENANT_ID,
                portfolio_id=REFERENCE_PORTFOLIO_ID,
                intraday_enabled=True,
            )
        )
        session.commit()
    elif pref.intraday_enabled is not True:
        pref.intraday_enabled = True
        session.commit()


def _ensure_reference_display_name_current(session: Session) -> None:
    """Force ``Portfolio.name`` (and the live sleeve's own ``Account.name``) to
    ``REFERENCE_PORTFOLIO_NAME``, unconditionally, on every call.

    Both are set ONLY at first creation elsewhere — ``Portfolio.name`` in
    ``ensure_reference_seeded``'s create branch above, ``Account.name`` in
    ``persistence._upsert_accounts`` (whose re-sync branch explicitly never touches
    ``name``, by design, so a Settings edit survives re-imports) — so renaming the
    constant alone would silently do nothing for an already-deployed instance still
    holding the pre-rename "Reference Rate" name."""
    portfolio = session.get(models.Portfolio, REFERENCE_PORTFOLIO_ID)
    if portfolio is not None and portfolio.name != REFERENCE_PORTFOLIO_NAME:
        portfolio.name = REFERENCE_PORTFOLIO_NAME
        session.commit()
    live_account = session.scalars(
        select(models.Account).where(
            models.Account.portfolio_id == REFERENCE_PORTFOLIO_ID,
            models.Account.broker == reference_connector.SOURCE,
        )
    ).first()
    if live_account is not None and live_account.name != REFERENCE_PORTFOLIO_NAME:
        live_account.name = REFERENCE_PORTFOLIO_NAME
        session.commit()


def sync_reference_holdings(session: Session, *, reader=None) -> bool:
    """Sync the Showcase Portfolio's live sleeve from the published artifact. Idempotent.

    Reads ``metron/reference_rate.json`` (``reader`` injectable for tests), maps it
    through the same canonical bridge every connector uses (positions →
    ``persist_snapshot``), applies per-position sector, and upserts the NAV-vs-SPY
    history into ``NavSnapshot``. Fail-soft: a missing/unreadable artifact leaves the
    last-good showcase untouched and returns False — and never materializes an empty
    portfolio (the shell is created only once a real artifact is in hand). Returns True
    when holdings were synced."""
    artifact = (reader or reference_connector.read_reference_artifact)()
    if not artifact:
        return False
    snapshot = reference_connector.artifact_to_snapshot(artifact)
    if not snapshot.holdings:
        return False

    ensure_reference_seeded(session)
    persistence.persist_snapshot(
        session,
        tenant_id=DEMO_TENANT_ID,
        portfolio_id=REFERENCE_PORTFOLIO_ID,
        snapshot=snapshot,
    )
    _apply_reference_sectors(session, artifact.get("positions") or [])
    _seed_reference_nav(session, artifact)
    session.commit()
    return True


def _apply_reference_sectors(session: Session, positions: list[dict]) -> None:
    """Set sector (and a display name) on the reference securities from the artifact
    (the snapshot path leaves sector unset)."""
    by_ticker = {(p.get("ticker") or "").strip(): p for p in positions if p.get("ticker")}
    if not by_ticker:
        return
    rows = session.scalars(
        select(models.Security).where(models.Security.symbol.in_(list(by_ticker)))
    ).all()
    for sec in rows:
        pos = by_ticker.get(sec.symbol)
        if pos and pos.get("sector"):
            sec.sector = pos["sector"]


def _seed_reference_nav(session: Session, artifact: dict) -> None:
    """Upsert the NAV-vs-SPY history into ``NavSnapshot`` (idempotent by snap_date) — the
    SOLE writer of this portfolio's NAV series. ``daily_refresh`` excludes
    ``REFERENCE_PORTFOLIO_ID`` from its generic per-portfolio NAV writers
    (``record_snapshot`` et al.) precisely so nothing re-derives and clobbers what's
    written here from Metron's own price/FX cache (metron-ops#141) — every row this
    portfolio ever has comes from this function alone.

    Each point's nav/cost_basis fold in the frozen sample sleeve's own constant totals
    (``_sample_sleeve_totals``) so the persisted portfolio-level series stays consistent
    with the combined Holdings total, not just the live sleeve's artifact-reported value.

    ``external_flow`` is 0 — the showcase models no external contributions, so the
    flow-neutralized return series IS the portfolio return (the illustrative alpha-vs-
    SPY curve). ``cost_basis`` is the current total cost basis (constant; it only feeds
    the total-return-vs-cost display, not the TWR curve)."""
    history = artifact.get("nav_history") or []
    if not history:
        return
    live_total_cost = sum(
        abs((p.get("shares") or 0) * (p.get("avg_cost") or 0))
        for p in (artifact.get("positions") or [])
    )
    sample_value, sample_cost_basis = _sample_sleeve_totals(session)
    total_cost = live_total_cost + sample_cost_basis
    existing = {
        row.snap_date: row
        for row in session.scalars(
            select(models.NavSnapshot).where(
                models.NavSnapshot.portfolio_id == REFERENCE_PORTFOLIO_ID
            )
        ).all()
    }
    for point in history:
        raw_date = point.get("date")
        live_nav = point.get("nav")
        if raw_date is None or live_nav is None:
            continue
        nav = live_nav + sample_value
        snap_date = date.fromisoformat(str(raw_date)[:10])
        spy_close = point.get("spy_close")
        row = existing.get(snap_date)
        if row is None:
            session.add(
                models.NavSnapshot(
                    tenant_id=DEMO_TENANT_ID,
                    portfolio_id=REFERENCE_PORTFOLIO_ID,
                    snap_date=snap_date,
                    nav=nav,
                    cost_basis=total_cost or nav,
                    external_flow=0.0,
                    spy_close=spy_close,
                )
            )
        else:
            row.nav = nav
            row.cost_basis = total_cost or nav
            if spy_close is not None:
                row.spy_close = spy_close
