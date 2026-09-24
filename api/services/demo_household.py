"""Demo household — the ICP-shaped demo portfolio (metron-ops-I317).

The Showcase Portfolio (``api/services/demo.py``) is Crucible's live single-account
equity book plus a two-account non-equity sleeve; it doesn't look like the lead
segment's own portfolio (retire-early / active self-directed US investors —
build-plan §5.1 A1, ``metron-ops`` build-plan-260915.md §1.5, §5.1). This module
seeds a SECOND, separate demo portfolio shaped like that segment: three accounts
across three "brokers" (a taxable brokerage, a Roth IRA, a 401k), ~25 holdings (US
equities, two index ETFs, one bond fund), a 5-year transaction history with monthly
contributions, dividends (``DEMO-KO``'s are reinvested — see below), two realized sales — one a wash-sale
window (a loss sale followed by a repurchase of the same symbol within 30 days), the
other straddling the one-year short/long-term boundary in a single FIFO relief — and
an uninvested cash balance.

Follows the Showcase's ``_reconcile_sample_sleeve`` pattern exactly, at a larger
scale: the fixture CSV is replayed through the same CSV-import bridge a real upload
uses on every startup (union-by-source_key ADD, explicit-delete REMOVE), so an
already-deployed instance self-heals onto a later fixture edit. Two differences from
the Showcase sleeve:

  * This is a full second **portfolio** (own id, own tenant-visible entry), not a
    sleeve folded into an existing one — it needs its own realistic performance
    history, which the Showcase's single frozen price-as-of can't provide.
  * NAV history is BUILT, not copied from a live artifact. ``_seed_price_history_and_nav``
    replays the fixture's month-end close fixture (``fixtures/demo_household/closes.csv``)
    and the real ``performance.record_snapshot`` engine call, one month at a time, in
    chronological order — the same engine path a real tenant's daily refresh uses.
    Prices are inserted one month ahead of the matching ``record_snapshot`` call
    (never all up front) because valuation always reads the GLOBALLY latest cached
    price bar per symbol (``prices.latest_close_by_symbol``): inserting the whole
    5-year price history before replay would make every historical snapshot value
    holdings at the FINAL month's price instead of that month's own. Idempotent per
    month (skips a month whose ``NavSnapshot`` already exists), so a redeploy is a
    cheap no-op walk over already-seeded months and a fixture extended with new
    trailing months only computes the new ones — it must never re-run
    ``record_snapshot`` for an already-seeded month, since by then the "latest price"
    for early months is no longer that month's own price.

Prices are a committed, deterministic synthetic walk (base + drift + bounded
sinusoidal wobble, seeded per symbol) — never fetched from any vendor
(``test_app_code_never_imports_yfinance``); see ``fixtures/demo_household/closes.csv``.

Every fixture symbol is written under a reserved ``DEMO-`` namespace (``DEMO-AAPL``,
not ``AAPL``) — see ``DEMO_SYMBOL_PREFIX``. ``securities`` and ``price_bars`` are
GLOBAL, cross-tenant tables (api/db/models.py); a bare real ticker here would be the
SAME row a real tenant's real holding in that ticker reads, so a synthetic close or an
overwritten name/sector would leak into every real tenant's TWR/risk/shadow-recompute/
market-board series for that symbol (a metron-ops#201-class defect — found and fixed
in this PR's own review before merge). ``_apply_security_meta`` and
``_seed_price_bars_for_date`` both hard-refuse (raise) any non-namespaced symbol.

Writes are refused via ``demo.assert_writable`` (same guard, same demo tenant) so the
household can never be mutated by a visitor. Visible on every real tenant's dashboard
the same way the Showcase is — see ``api/routers/portfolios.py::list_portfolios`` /
``_owned_portfolio`` and the ``_demo_read_only`` HTTP-layer guard in ``api/main.py``.

Dividend reinvestment (DRP, metron-ops-I325). ``DEMO-KO``'s twenty quarterly
dividends each carry a same-date ``REINVESTMENT`` row in
``fixtures/demo_household/transactions.csv``, priced at that date's own close from
``closes.csv``, for ``dividend_amount / close`` fractional shares.
``csv_import._TYPE_SYNONYMS`` maps ``reinvestment``/``reinvest shares`` onto
``TxnType.REINVESTMENT`` (metron-ops#335), so a DRP is a cash ``DIVIDEND`` followed by a
``REINVESTMENT`` of the same symbol on the same date — a first-class type every layer
treats exactly as a ``BUY`` (``TxnType.is_purchase``) and the Transactions table renders
as a reinvested dividend. An instance seeded before that type existed holds these rows
as ``BUY``; the next reconcile re-types them in place (the legacy-key path in
``persistence._insert_activities``) rather than duplicating them.

Its TWR treatment is correct by construction, and not the one the phrase "not an
external contribution" first suggests: Metron's NAV is the market value of HOLDINGS
only, with no cash bucket, so ``performance._net_purchases`` defines
``NavSnapshot.external_flow`` as NET PURCHASES (ΣBUY − ΣSELL) rather than as cash
deposits — "a reinvested dividend is a buy" (metron-ops#44). The dividend cash was
never inside the valued NAV, so the reinvestment is the moment that capital enters and
is neutralised exactly like any other purchase; what must never happen is it being
booked as new OUTSIDE money (a DEPOSIT).
``tests/test_demo_household.py::TestDividendReinvestment`` pins the share-count
increase and recomputes the whole flow series from the fixture to hold that line;
``TestReinvestmentType`` pins that the rows persist and render as ``REINVESTMENT`` and
that the TWR and attribution goldens are unchanged by the type (metron-ops#335).

Goal inputs (metron-ops-I317 deliverable 4): illustrative retirement-goal values
(target amount, target date, annual contribution, withdrawal rate) are upserted onto
this portfolio ONLY, on every reconcile, via ``goal.set_goal`` — see
``_reconcile_goal`` below. A real tenant's portfolio never gets a default: nothing in
this module ever calls ``set_goal`` for any portfolio id other than
``DEMO_HOUSEHOLD_PORTFOLIO_ID``, and ``goal.set_goal`` itself defaults nothing (every
field is exactly what its caller passes) — the invariant
``tests/test_goal.py::TestGoalRouter::test_get_before_set_is_empty_no_prefill`` guards
for every other portfolio.
"""

from __future__ import annotations

import csv
import os
import uuid
from dataclasses import replace
from datetime import date

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from api.db import models
from api.services import goal as goal_service
from api.services import persistence
from api.services import plan_targets as plan_targets_service
from api.services.demo import DEMO_TENANT_ID
from api.services.demo_namespace import DEMO_SYMBOL_PREFIX as _DEMO_SYMBOL_PREFIX
from api.services.demo_namespace import assert_demo_symbols
from api.services.performance import record_snapshot
from portfolio_analytics.broker_io.csv_import import parse_transactions_csv
from portfolio_analytics.ingestion.schema import activity_key

# Fixed, well-known id (stable across restarts so links don't break) — distinct from
# demo.REFERENCE_PORTFOLIO_ID, a second portfolio under the SAME demo tenant.
DEMO_HOUSEHOLD_PORTFOLIO_ID = uuid.UUID("00000000-0000-0000-0000-00000000de63")
DEMO_HOUSEHOLD_PORTFOLIO_NAME = "Demo household (illustrative)"

# Distinct connector-source / broker label — every query below scopes to precisely
# this portfolio's own accounts, never the Showcase's live or sample sleeve.
_SOURCE = "demo_household"

_FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "demo_household")

# Reserved demo namespace — the prefix and both guards now live in the one shared
# module ``api/services/demo_namespace.py`` (second adoption: ``api/services/demo.py``'s
# Showcase sample sleeve moved under the same namespace in metron-ops-I319, and
# shared-code-policy says lift rather than copy). Re-exported here because this module's
# public name for it predates the split and tests/callers address it through this module.
# ``_seed_price_bars_for_date`` and ``_apply_security_meta`` both hard-refuse (raise) any
# symbol without the prefix — see their docstrings.
DEMO_SYMBOL_PREFIX = _DEMO_SYMBOL_PREFIX

# Per-symbol reference metadata applied after import (the CSV path defaults everything
# to equity, unnamed). (name, asset_class, sector — sector is None for the ETFs/bond
# fund, matching how a real fund is excluded from single-GICS-sector attribution).
# Names carry "(illustrative)" so the fixture never reads as a real quote/listing.
SECURITY_META: dict[str, tuple[str, str, str | None]] = {
    "DEMO-VTI": ("Vanguard Total Stock Market ETF (illustrative)", "etf", None),
    "DEMO-VOO": ("Vanguard S&P 500 ETF (illustrative)", "etf", None),
    "DEMO-BND": ("Vanguard Total Bond Market ETF (illustrative)", "bond", None),
    "DEMO-AAPL": ("Apple Inc. (illustrative)", "equity", "Technology"),
    "DEMO-MSFT": ("Microsoft Corp. (illustrative)", "equity", "Technology"),
    "DEMO-GOOGL": ("Alphabet Inc. Class A (illustrative)", "equity", "Communication Services"),
    "DEMO-AMZN": ("Amazon.com Inc. (illustrative)", "equity", "Consumer Cyclical"),
    "DEMO-JNJ": ("Johnson & Johnson (illustrative)", "equity", "Healthcare"),
    "DEMO-PG": ("Procter & Gamble Co. (illustrative)", "equity", "Consumer Defensive"),
    "DEMO-KO": ("Coca-Cola Co. (illustrative)", "equity", "Consumer Defensive"),
    "DEMO-XOM": ("Exxon Mobil Corp. (illustrative)", "equity", "Energy"),
    "DEMO-CVX": ("Chevron Corp. (illustrative)", "equity", "Energy"),
    "DEMO-JPM": ("JPMorgan Chase & Co. (illustrative)", "equity", "Financial Services"),
    "DEMO-BAC": ("Bank of America Corp. (illustrative)", "equity", "Financial Services"),
    "DEMO-HD": ("Home Depot Inc. (illustrative)", "equity", "Consumer Cyclical"),
    "DEMO-WMT": ("Walmart Inc. (illustrative)", "equity", "Consumer Defensive"),
    "DEMO-DIS": ("Walt Disney Co. (illustrative)", "equity", "Communication Services"),
    "DEMO-V": ("Visa Inc. (illustrative)", "equity", "Financial Services"),
    "DEMO-MA": ("Mastercard Inc. (illustrative)", "equity", "Financial Services"),
    "DEMO-UNH": ("UnitedHealth Group Inc. (illustrative)", "equity", "Healthcare"),
    "DEMO-COST": ("Costco Wholesale Corp. (illustrative)", "equity", "Consumer Defensive"),
    "DEMO-PEP": ("PepsiCo Inc. (illustrative)", "equity", "Consumer Defensive"),
    "DEMO-META": ("Meta Platforms Inc. (illustrative)", "equity", "Communication Services"),
    "DEMO-NVDA": ("NVIDIA Corp. (illustrative)", "equity", "Technology"),
    "DEMO-TSLA": ("Tesla Inc. (illustrative)", "equity", "Consumer Cyclical"),
}

# Illustrative retirement-goal inputs (metron-ops-I317 deliverable 4), sized for this
# fixture: current value is ~$242k (test_golden_attribution_input_sector_weights) —
# roughly 3x that as the target, a ~19-year horizon, a $12k/yr contribution matching
# the fixture's own monthly-DCA scale, and the textbook 4% withdrawal rate. Fictional,
# same as every other number this module seeds — never a suggestion to a real user
# (see this module's docstring and ``goal.set_goal``'s own no-default invariant).
DEMO_HOUSEHOLD_GOAL_TARGET_AMOUNT_USD = 750_000.0
DEMO_HOUSEHOLD_GOAL_TARGET_DATE = date(2046, 1, 1)
DEMO_HOUSEHOLD_GOAL_ANNUAL_CONTRIBUTION_USD = 12_000.0
DEMO_HOUSEHOLD_GOAL_WITHDRAWAL_RATE = 0.04

# Illustrative plan-target inputs (metron-ops-I322 deliverable 3), so "New cash to my
# targets" has something to compute against on first visit — the same "user typed
# this in" fiction as the goal inputs above: a real user's ``plan_targets`` row is
# NEVER defaulted (``goal.set_goal``'s no-default invariant, mirrored here — see
# ``tests/test_planning_router.py::TestTargetsRoundTrip::
# test_get_with_nothing_saved_is_an_empty_no_default_state``, which this module never
# touches for any portfolio but the household). Thirteen of the fixture's own
# ``DEMO-`` symbols, weights summing to 0.98 (<=1, leaving headroom for "new cash"),
# a 20% single-position cap (equal to the largest single weight below — a cap, not a
# floor, so it excludes nothing) and a $250 minimum line.
DEMO_HOUSEHOLD_PLAN_TARGETS: tuple[tuple[str, float], ...] = (
    ("DEMO-VTI", 0.20),
    ("DEMO-VOO", 0.15),
    ("DEMO-BND", 0.10),
    ("DEMO-AAPL", 0.08),
    ("DEMO-MSFT", 0.08),
    ("DEMO-GOOGL", 0.06),
    ("DEMO-AMZN", 0.06),
    ("DEMO-JNJ", 0.05),
    ("DEMO-JPM", 0.05),
    ("DEMO-V", 0.05),
    ("DEMO-NVDA", 0.04),
    ("DEMO-COST", 0.03),
    ("DEMO-XOM", 0.03),
)
DEMO_HOUSEHOLD_PLAN_MAX_SINGLE_POSITION = 0.20
DEMO_HOUSEHOLD_PLAN_MIN_LINE_USD = 250.0

# The three accounts (external_id -> (tax_treatment, account_type)) — a taxable
# brokerage (tax_treatment left None; derives to "Taxable"), a Roth IRA (tax_exempt —
# gains never realized for tax purposes), and a 401(k) (tax_deferred — withdrawals
# taxed as ordinary income). Three distinct "brokers" per the ICP (viability §2:
# taxable + IRA + 401k across 3+ institutions); each account's own broker string
# stands in for a distinct institution without naming a real one.
ACCOUNT_META: dict[str, tuple[str | None, str]] = {
    "Demo Taxable Brokerage": (None, "Brokerage"),
    "Demo Roth IRA": ("tax_exempt", "Roth IRA"),
    "Demo 401k": ("tax_deferred", "401(k)"),
}


def _load_transactions_csv() -> str:
    with open(os.path.join(_FIXTURE_DIR, "transactions.csv")) as f:
        return f.read()


def _load_monthly_closes() -> dict[date, dict[str, float]]:
    """``{month_end_date: {symbol: close}}``, sorted read order not guaranteed — callers
    sort the keys themselves (``sorted(...)`` on a date dict is cheap and explicit)."""
    out: dict[date, dict[str, float]] = {}
    with open(os.path.join(_FIXTURE_DIR, "closes.csv")) as f:
        for row in csv.DictReader(f):
            d = date.fromisoformat(row["date"])
            out.setdefault(d, {})[row["symbol"]] = float(row["close"])
    return out


def ensure_demo_household_seeded(session: Session) -> bool:
    """Idempotently create the Demo household portfolio shell under the demo tenant,
    then reconcile its transactions and NAV history unconditionally on every call —
    safe to call on every startup, mirroring ``demo.ensure_reference_seeded``.

    Returns True if it created the portfolio shell this call, False if it already
    existed."""
    created = False
    if session.get(models.Portfolio, DEMO_HOUSEHOLD_PORTFOLIO_ID) is None:
        if session.get(models.Tenant, DEMO_TENANT_ID) is None:
            session.add(models.Tenant(id=DEMO_TENANT_ID, name="Demo"))
        session.add(
            models.Portfolio(
                id=DEMO_HOUSEHOLD_PORTFOLIO_ID,
                tenant_id=DEMO_TENANT_ID,
                name=DEMO_HOUSEHOLD_PORTFOLIO_NAME,
                base_currency="USD",
            )
        )
        session.commit()
        created = True
    _reconcile_and_backfill(session)
    return created


def _reconcile_and_backfill(session: Session) -> None:
    """Bidirectional reconcile against the current fixture (ADD via the CSV-import
    bridge, REMOVE via an explicit prune — mirrors ``demo._reconcile_sample_sleeve``),
    interleaved MONTH BY MONTH with the price/NAV backfill.

    Transactions cannot be persisted all at once up front the way the Showcase's
    frozen sample sleeve is: ``analytics.holdings``/``record_snapshot`` always value
    whatever is CURRENTLY persisted (there is no "as of" filter), so persisting the
    full 5-year history before replaying month 1 would value month 1's snapshot off
    5 years of already-bought shares. Instead, each month's activities are persisted
    only once the replay reaches that month, immediately followed by that month's
    price bars and its ``record_snapshot`` call — see ``_seed_price_history_and_nav``.
    """
    text = _load_transactions_csv()
    result = parse_transactions_csv(text, source=_SOURCE)
    _seed_price_history_and_nav(session, result)
    _apply_security_meta(session)
    _apply_account_meta(session)
    _prune_retired_activities(session, result)
    _reconcile_goal(session)
    _reconcile_plan_targets(session)
    session.commit()


def _reconcile_goal(session: Session) -> None:
    """Idempotent upsert of the illustrative goal inputs onto the demo household ONLY
    (metron-ops-I317 deliverable 4) — ``goal.set_goal`` is itself a full-replace
    upsert (one row per portfolio, create-or-update), so calling it on every reconcile
    is the same bidirectional-propagation shape as the rest of this module: a later
    edit to the constants above lands on an already-deployed instance the next time it
    reconciles, without ever touching any other portfolio's goal row."""
    goal_service.set_goal(
        session,
        DEMO_TENANT_ID,
        DEMO_HOUSEHOLD_PORTFOLIO_ID,
        target_amount_usd=DEMO_HOUSEHOLD_GOAL_TARGET_AMOUNT_USD,
        target_date=DEMO_HOUSEHOLD_GOAL_TARGET_DATE,
        annual_contribution_usd=DEMO_HOUSEHOLD_GOAL_ANNUAL_CONTRIBUTION_USD,
        withdrawal_rate=DEMO_HOUSEHOLD_GOAL_WITHDRAWAL_RATE,
    )


def _reconcile_plan_targets(session: Session) -> None:
    """Idempotent upsert of the illustrative plan-target inputs onto the demo
    household ONLY (metron-ops-I322 deliverable 3) — ``plan_targets.set_plan_targets``
    is a full-replace upsert (one row per portfolio, create-or-update), so calling it
    on every reconcile is the same bidirectional-propagation shape as
    ``_reconcile_goal`` above: a later edit to ``DEMO_HOUSEHOLD_PLAN_TARGETS`` lands on
    an already-deployed instance the next reconcile, without ever touching any other
    portfolio's plan-targets row."""
    plan_targets_service.set_plan_targets(
        session,
        DEMO_TENANT_ID,
        DEMO_HOUSEHOLD_PORTFOLIO_ID,
        targets=[{"symbol": symbol, "weight": weight} for symbol, weight in DEMO_HOUSEHOLD_PLAN_TARGETS],
        max_single_position=DEMO_HOUSEHOLD_PLAN_MAX_SINGLE_POSITION,
        min_line_usd=DEMO_HOUSEHOLD_PLAN_MIN_LINE_USD,
    )


def _apply_security_meta(session: Session) -> None:
    """Overwrite name/asset_class/sector on this fixture's own securities — GUARDED:
    ``securities`` is a GLOBAL, cross-tenant table, so writing metadata onto a
    non-namespaced symbol here would silently overwrite the name/sector of whatever
    REAL tenant's holding shares that ticker (metron-ops#201-class defect, found in
    PR464 review). Raises rather than skipping — a symbol reaching this function
    without the ``DEMO-`` prefix is a fixture-authoring bug that must be fixed, not
    silently dropped."""
    assert_demo_symbols(SECURITY_META, context="demo_household.SECURITY_META")
    rows = session.scalars(select(models.Security).where(models.Security.symbol.in_(list(SECURITY_META)))).all()
    assert_demo_symbols(
        (sec.symbol for sec in rows), context="demo_household._apply_security_meta (matched Security rows)"
    )
    for sec in rows:
        meta = SECURITY_META.get(sec.symbol)
        if meta:
            sec.name, sec.asset_class, sec.sector = meta


def _apply_account_meta(session: Session) -> None:
    rows = session.scalars(
        select(models.Account).where(
            models.Account.portfolio_id == DEMO_HOUSEHOLD_PORTFOLIO_ID, models.Account.broker == _SOURCE
        )
    ).all()
    for acct in rows:
        meta = ACCOUNT_META.get(acct.external_id)
        if meta:
            acct.tax_treatment, acct.account_type = meta


def _prune_retired_activities(session: Session, result) -> None:
    """Delete any persisted transaction whose ``source_key`` is no longer produced by
    the current fixture — the REMOVE half of the reconcile (mirrors
    ``demo._prune_retired_sample_sleeve_holdings``, keyed on the full activity, not
    just the symbol, since this fixture's identity is transaction-grained)."""
    account_ids = list(
        session.scalars(
            select(models.Account.id).where(
                models.Account.portfolio_id == DEMO_HOUSEHOLD_PORTFOLIO_ID, models.Account.broker == _SOURCE
            )
        ).all()
    )
    if not account_ids:
        return
    current_keys = {activity_key(act) for act in result.snapshot.activities}
    if not current_keys:
        return  # never wipe everything off an empty/unparseable fixture read
    session.execute(
        delete(models.Transaction).where(
            models.Transaction.account_id.in_(account_ids),
            models.Transaction.source_key.not_in(current_keys),
        )
    )


# No-op price source injected into ``record_snapshot`` for the SPY comparison close —
# this seeding path must never reach the data spine / network (the repo-wide "no
# vendor fetch" rule; app code imports no yfinance at all,
# ``test_app_code_never_imports_yfinance``). Fail-soft: an absent SPY close just
# leaves ``NavSnapshot.spy_close`` unset for these historical rows, same as any other
# symbol the price source can't resolve.
def _no_spy_source(symbols: list[str]) -> dict:
    return {}


def _seed_price_history_and_nav(session: Session, result) -> None:
    """Walk the fixture's 60 month-end dates in chronological order. Each month: (1)
    persist only the activities dated on/before this month that haven't been persisted
    yet (a growing prefix of the full activity list — the accounts/securities lists
    stay the FULL fixture set on every call, cheap to re-upsert and needed so an
    early-month price bar can resolve a security not yet referenced by any activity);
    (2) upsert that month's price bars; (3) ``record_snapshot`` for that month, unless
    it's already recorded. This keeps "what's persisted" and "what's priced as latest"
    in lockstep with "what date we're valuing", which the engine itself has no
    as-of-date concept to do for us (see ``_reconcile_and_backfill``'s docstring).

    Idempotent per month: a month whose ``NavSnapshot`` already exists is skipped for
    the ``record_snapshot`` call (price bars and transactions for it still upsert, in
    case a fixture edit changed their values without adding a new trailing month), so
    an already-deployed instance never re-derives a historical month's NAV against a
    since-advanced "latest" price."""
    monthly_closes = _load_monthly_closes()
    existing_snapshot_dates = set(
        session.scalars(
            select(models.NavSnapshot.snap_date).where(
                models.NavSnapshot.tenant_id == DEMO_TENANT_ID,
                models.NavSnapshot.portfolio_id == DEMO_HOUSEHOLD_PORTFOLIO_ID,
            )
        ).all()
    )
    activities_sorted = sorted(result.snapshot.activities, key=lambda a: a.when)
    idx = 0
    n = len(activities_sorted)
    for d in sorted(monthly_closes):
        batch = []
        while idx < n and activities_sorted[idx].when <= d:
            batch.append(activities_sorted[idx])
            idx += 1
        if batch:
            sub_snapshot = replace(result.snapshot, activities=batch)
            persistence.persist_snapshot(
                session, tenant_id=DEMO_TENANT_ID, portfolio_id=DEMO_HOUSEHOLD_PORTFOLIO_ID, snapshot=sub_snapshot
            )
        _seed_price_bars_for_date(session, d, monthly_closes[d])
        if d in existing_snapshot_dates:
            continue
        record_snapshot(session, DEMO_TENANT_ID, DEMO_HOUSEHOLD_PORTFOLIO_ID, today=d, source=_no_spy_source)
    # Any fixture activity dated AFTER the last priced month (shouldn't happen — the
    # fixture is generated to end before the last month — but persisted here too so a
    # future fixture edit that adds a late transaction without a matching close still
    # lands in the ledger rather than being silently dropped).
    if idx < n:
        sub_snapshot = replace(result.snapshot, activities=activities_sorted[idx:])
        persistence.persist_snapshot(
            session, tenant_id=DEMO_TENANT_ID, portfolio_id=DEMO_HOUSEHOLD_PORTFOLIO_ID, snapshot=sub_snapshot
        )


def _seed_price_bars_for_date(session: Session, d: date, closes: dict[str, float]) -> None:
    """Upsert one ``PriceBar`` per symbol for ``d`` — skip-if-exists (mirrors
    ``demo._seed_sample_sleeve_prices``), so a symbol/date pair already written by a
    prior startup is never re-priced out from under an already-recorded snapshot.

    GUARDED: ``price_bars`` is a GLOBAL, cross-tenant EOD cache keyed on
    ``security_id`` — every real tenant holding the same underlying ticker reads the
    SAME row. Writing a synthetic close under a bare "AAPL" here would inject a fake
    point into every real tenant's TWR/risk/shadow-recompute/market-board series for
    that symbol (metron-ops#201-class defect, found in PR464 review). Raises rather
    than skipping any non-``DEMO-``-namespaced symbol — a fixture-authoring bug, not a
    case to silently degrade."""
    assert_demo_symbols(closes, context=f"demo_household._seed_price_bars_for_date({d})")
    secs = {sec.symbol: sec for sec in session.scalars(
        select(models.Security).where(models.Security.symbol.in_(list(closes)))
    ).all()}
    existing = set(
        session.scalars(
            select(models.PriceBar.security_id).where(
                models.PriceBar.security_id.in_([s.id for s in secs.values()]),
                models.PriceBar.bar_date == d,
            )
        ).all()
    )
    for symbol, close in closes.items():
        sec = secs.get(symbol)
        if sec is None or sec.id in existing:
            continue
        session.add(models.PriceBar(security_id=sec.id, bar_date=d, close=close, currency=sec.currency or "USD"))
    session.commit()
