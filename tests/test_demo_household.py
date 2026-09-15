"""Demo household (metron-ops-I317) — the ICP-shaped demo portfolio: three accounts
(taxable brokerage, Roth IRA, 401k), ~25 holdings, a 5-year history with monthly
contributions, dividends, two realized sales (a wash-sale window + a lot straddling
the one-year ST/LT boundary), and a cash balance. Golden-file tests pin TWR, MWR,
holdings totals, sector-weighted attribution inputs, and the two realized lots against
independently-recomputed numbers (never eyeballed) — plus idempotency, bidirectional
fixture-edit propagation, and read-only enforcement, mirroring ``tests/test_demo.py``.
"""

from __future__ import annotations

import io
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from api.db import models
from api.services import analytics, demo_household
from api.services import performance as perf
from api.services.analytics import _cash_by_account

DEMO_HEADERS = {"X-Tenant-Id": str(demo_household.DEMO_TENANT_ID)}


def _taxable_account(session) -> models.Account:
    return session.scalars(
        select(models.Account).where(
            models.Account.portfolio_id == demo_household.DEMO_HOUSEHOLD_PORTFOLIO_ID,
            models.Account.external_id == "Demo Taxable Brokerage",
        )
    ).one()


# ── Seeding: idempotency, bidirectional reconcile, shell creation ───────────────


def test_seed_creates_shell_once(db_session):
    assert demo_household.ensure_demo_household_seeded(db_session) is True
    assert demo_household.ensure_demo_household_seeded(db_session) is False


def test_seed_is_idempotent(db_session):
    demo_household.ensure_demo_household_seeded(db_session)
    tx_count_1 = len(db_session.scalars(select(models.Transaction.id)).all())
    nav_1 = [
        (n.snap_date, float(n.nav))
        for n in db_session.scalars(
            select(models.NavSnapshot)
            .where(models.NavSnapshot.portfolio_id == demo_household.DEMO_HOUSEHOLD_PORTFOLIO_ID)
            .order_by(models.NavSnapshot.snap_date)
        ).all()
    ]

    demo_household.ensure_demo_household_seeded(db_session)  # re-seed must not duplicate
    tx_count_2 = len(db_session.scalars(select(models.Transaction.id)).all())
    nav_2 = [
        (n.snap_date, float(n.nav))
        for n in db_session.scalars(
            select(models.NavSnapshot)
            .where(models.NavSnapshot.portfolio_id == demo_household.DEMO_HOUSEHOLD_PORTFOLIO_ID)
            .order_by(models.NavSnapshot.snap_date)
        ).all()
    ]
    assert tx_count_1 == tx_count_2
    assert nav_1 == nav_2  # historical months are never re-derived against a moved "latest" price
    assert len(nav_1) == 60  # one per fixture month


def test_three_accounts_three_tax_treatments(db_session):
    demo_household.ensure_demo_household_seeded(db_session)
    accounts = db_session.scalars(
        select(models.Account).where(models.Account.portfolio_id == demo_household.DEMO_HOUSEHOLD_PORTFOLIO_ID)
    ).all()
    by_name = {a.external_id: (a.tax_treatment, a.account_type) for a in accounts}
    assert by_name == {
        "Demo Taxable Brokerage": (None, "Brokerage"),
        "Demo Roth IRA": ("tax_exempt", "Roth IRA"),
        "Demo 401k": ("tax_deferred", "401(k)"),
    }


def test_twenty_five_holdings_across_equities_etfs_and_a_bond_fund(db_session):
    demo_household.ensure_demo_household_seeded(db_session)
    held = analytics.holdings(db_session, demo_household.DEMO_TENANT_ID, demo_household.DEMO_HOUSEHOLD_PORTFOLIO_ID)
    assert len(held) == 25
    by_class = {}
    for h in held:
        sec = db_session.scalars(select(models.Security).where(models.Security.symbol == h.ticker)).one()
        by_class[sec.asset_class] = by_class.get(sec.asset_class, 0) + 1
    assert by_class["bond"] == 1  # BND, the one bond fund
    assert by_class["etf"] == 2  # VTI + VOO
    assert by_class["equity"] == 22


def test_fixture_edit_propagates_bidirectionally(db_session):
    """metron-ops-I201-style regression guard (see ``test_demo.py``'s equivalent for
    the Showcase): a row no longer produced by the current fixture (e.g. a
    since-retired symbol from an older fixture version) must be pruned on the next
    ``ensure_demo_household_seeded`` call, not left to accumulate forever."""
    demo_household.ensure_demo_household_seeded(db_session)
    taxable = _taxable_account(db_session)
    security = models.Security(symbol="ZZZZ", name="Retired Test Corp", currency="USD", asset_class="equity")
    db_session.add(security)
    db_session.commit()
    stale = models.Transaction(
        tenant_id=demo_household.DEMO_TENANT_ID,
        account_id=taxable.id,
        security_id=security.id,
        txn_type="BUY",
        quantity=1,
        price=1.0,
        fees=0.0,
        amount=1.0,
        currency="USD",
        trade_date=date(2022, 1, 1),
        source_key="stale-pre-fixture-row-not-in-current-csv",
    )
    db_session.add(stale)
    db_session.commit()

    demo_household.ensure_demo_household_seeded(db_session)  # must prune the stale row

    remaining = db_session.scalars(
        select(models.Transaction).where(models.Transaction.source_key == "stale-pre-fixture-row-not-in-current-csv")
    ).first()
    assert remaining is None


# ── Read-only enforcement (mirrors test_demo.py) ─────────────────────────────────


def test_demo_household_is_read_only_refresh(client, db_session):
    demo_household.ensure_demo_household_seeded(db_session)
    r = client.post(
        f"/portfolios/{demo_household.DEMO_HOUSEHOLD_PORTFOLIO_ID}/prices/refresh", headers=DEMO_HEADERS
    )
    assert r.status_code == 403


def test_demo_household_is_read_only_import(client, db_session):
    demo_household.ensure_demo_household_seeded(db_session)
    csv_body = "date,type,symbol,quantity,price,amount,account\n2024-01-01,BUY,TSLA,1,100,100,Hijack\n"
    r = client.post(
        f"/portfolios/{demo_household.DEMO_HOUSEHOLD_PORTFOLIO_ID}/import/csv",
        files={"file": ("t.csv", io.BytesIO(csv_body.encode()), "text/csv")},
        headers=DEMO_HEADERS,
    )
    assert r.status_code == 403


def test_demo_household_is_read_only_patch(client, db_session):
    demo_household.ensure_demo_household_seeded(db_session)
    r = client.patch(
        f"/portfolios/{demo_household.DEMO_HOUSEHOLD_PORTFOLIO_ID}", json={"name": "Hijacked"}, headers=DEMO_HEADERS
    )
    assert r.status_code == 403


def test_demo_household_visible_on_every_real_tenant(client, db_session):
    demo_household.ensure_demo_household_seeded(db_session)
    import uuid

    tenant = str(uuid.uuid4())
    r = client.get("/portfolios", headers={"X-Tenant-Id": tenant})
    assert r.status_code == 200
    ids = {p["id"] for p in r.json()}
    assert str(demo_household.DEMO_HOUSEHOLD_PORTFOLIO_ID) in ids


# ── Golden: TWR / MWR, independently re-derived from the persisted NAV series ────


def test_golden_twr_mwr(db_session):
    demo_household.ensure_demo_household_seeded(db_session)
    summary = perf.performance(db_session, demo_household.DEMO_TENANT_ID, demo_household.DEMO_HOUSEHOLD_PORTFOLIO_ID)
    assert summary.n_snapshots == 60
    assert summary.twr == pytest.approx(0.4446254377308594, abs=1e-9)
    assert summary.mwr == pytest.approx(0.507984300992856, abs=1e-6)
    assert summary.annualized_twr == pytest.approx(0.07766816183674763, abs=1e-6)

    # Independent re-derivation from the raw NavSnapshot series (the SAME documented
    # formula ``performance.py`` uses — period return = (nav - flow) / prev_nav - 1,
    # geometric-linked — computed here from scratch rather than calling into the
    # module under test, so this isn't just re-asserting the engine's own output.
    navs = db_session.scalars(
        select(models.NavSnapshot)
        .where(models.NavSnapshot.portfolio_id == demo_household.DEMO_HOUSEHOLD_PORTFOLIO_ID)
        .order_by(models.NavSnapshot.snap_date)
    ).all()
    cum = 1.0
    for i in range(1, len(navs)):
        prev_nav = float(navs[i - 1].nav)
        nav, flow = float(navs[i].nav), float(navs[i].external_flow)
        cum *= (nav - flow) / prev_nav
    independent_twr = cum - 1.0
    assert independent_twr == pytest.approx(summary.twr, abs=1e-9)


# ── Golden: holdings totals, independently summed from the committed fixture ─────


def test_golden_holdings_totals_independent_of_the_sale_symbols(db_session):
    """For every symbol NEVER sold, total held quantity = Σ BUY qty from the raw
    fixture CSV (no FIFO relief to reason about) — computed here directly from the
    committed CSV, independent of ``build_ledger``, and checked against the API."""
    demo_household.ensure_demo_household_seeded(db_session)
    text = demo_household._load_transactions_csv()
    import csv as _csv

    qty_by_symbol: dict[str, float] = {}
    for row in _csv.DictReader(text.splitlines()):
        sym = row["symbol"]
        if not sym:
            continue
        q = float(row["quantity"])
        if row["type"] == "BUY":
            qty_by_symbol[sym] = qty_by_symbol.get(sym, 0.0) + q
        elif row["type"] == "SELL":
            qty_by_symbol[sym] = qty_by_symbol.get(sym, 0.0) - q

    held = {
        h.ticker: h.quantity
        for h in analytics.holdings(db_session, demo_household.DEMO_TENANT_ID, demo_household.DEMO_HOUSEHOLD_PORTFOLIO_ID)
    }
    never_sold = [s for s in qty_by_symbol if s not in ("DEMO-DIS", "DEMO-AAPL")]
    assert len(never_sold) == 23
    for sym in never_sold:
        assert held[sym] == pytest.approx(qty_by_symbol[sym], rel=1e-6), sym


def test_golden_cash_balance(db_session):
    """The uninvested emergency-fund deposit ($4,200, month 60) plus the small DCA
    rounding residue leaves the taxable account's cash balance positive — the ICP
    fixture's ``cash balance`` requirement. Pinned + independently re-summed from the
    raw fixture CSV (Σ DEPOSIT + Σ DIVIDEND + Σ SELL − Σ BUY, taxable account only)."""
    demo_household.ensure_demo_household_seeded(db_session)
    taxable = _taxable_account(db_session)
    cash = _cash_by_account(
        db_session, demo_household.DEMO_TENANT_ID, demo_household.DEMO_HOUSEHOLD_PORTFOLIO_ID,
        account_ids=[taxable.id],
    )[taxable.id]

    text = demo_household._load_transactions_csv()
    import csv as _csv

    totals = {"DEPOSIT": 0.0, "BUY": 0.0, "SELL": 0.0, "DIVIDEND": 0.0}
    for row in _csv.DictReader(text.splitlines()):
        if row["account"] != "Demo Taxable Brokerage":
            continue
        totals[row["type"]] = totals.get(row["type"], 0.0) + float(row["amount"])
    independent_cash = totals["DEPOSIT"] + totals["DIVIDEND"] + totals["SELL"] - totals["BUY"]

    assert cash == pytest.approx(independent_cash, abs=1e-2)
    assert cash > 0


# ── Golden: the two realized sales — wash-sale window + ST/LT straddle ───────────


def test_golden_wash_sale_window(db_session):
    """DIS: bought 2022-12-15 @ $146.75 (8 sh of the lot sold below), sold 2025-05-15
    @ $141.93 — a LOSS (146.75 > 141.93) — then repurchased 2025-06-02 (18 days later,
    inside the 30-day wash-sale window). Hand-verified: cost basis = 8 * 146.75 =
    $1,174.00 exactly; proceeds = 8 * 141.93 = $1,135.44; gain = -$38.56."""
    demo_household.ensure_demo_household_seeded(db_session)
    realized = analytics.realized(db_session, demo_household.DEMO_TENANT_ID, demo_household.DEMO_HOUSEHOLD_PORTFOLIO_ID)
    dis_lots = [r for r in realized if r.ticker == "DEMO-DIS"]
    assert len(dis_lots) == 1
    lot = dis_lots[0]
    assert lot.quantity == pytest.approx(8.0)
    assert lot.cost_basis == pytest.approx(1174.00, abs=0.01)
    assert lot.proceeds == pytest.approx(1135.44, abs=0.01)
    assert lot.gain == pytest.approx(-38.56, abs=0.01)
    assert lot.gain < 0  # the wash-sale precondition: a LOSS sale

    # Structural check: a repurchase of the SAME symbol within 30 days of the sale —
    # the wash-sale window itself (the detector consuming this is separate scope,
    # metron-ops build-plan §5.1 A8; this asserts the fixture actually SHAPES one).
    rebuys = db_session.scalars(
        select(models.Transaction)
        .join(models.Security, models.Transaction.security_id == models.Security.id)
        .where(models.Security.symbol == "DEMO-DIS", models.Transaction.txn_type == "BUY")
        .order_by(models.Transaction.trade_date)
    ).all()
    window_rebuys = [
        t for t in rebuys
        if lot.close_date < t.trade_date <= lot.close_date + timedelta(days=30)
    ]
    assert len(window_rebuys) == 1
    assert (window_rebuys[0].trade_date - lot.close_date).days == 18


def test_golden_lot_straddling_the_one_year_boundary(db_session):
    """AAPL: lot 1 opened 2022-03-15 (16.6047 sh @ $150.56), lot 2 opened 2024-09-15
    (10.9452 sh @ $228.41). A single SELL of 23.2466 sh on 2025-08-15 FIFO-relieves
    ALL of lot 1 (held ~3.4yr -> long-term) and PART of lot 2 (held ~11mo ->
    short-term) — one sale straddling the one-year ST/LT boundary."""
    demo_household.ensure_demo_household_seeded(db_session)
    realized = analytics.realized(db_session, demo_household.DEMO_TENANT_ID, demo_household.DEMO_HOUSEHOLD_PORTFOLIO_ID)
    aapl_lots = sorted((r for r in realized if r.ticker == "DEMO-AAPL"), key=lambda r: r.open_date)
    assert len(aapl_lots) == 2

    lot1, lot2 = aapl_lots
    assert lot1.quantity == pytest.approx(16.6047)
    assert lot1.long_term is True
    assert lot1.gain > 0  # bought low, sold high — a long-term gain

    assert lot2.quantity == pytest.approx(23.2466 - 16.6047, abs=1e-4)
    assert lot2.long_term is False
    assert (lot1.close_date - lot2.open_date).days < 365  # short-term by construction

    # Both portions close on the SAME sale date — one SELL, two tax characters.
    assert lot1.close_date == lot2.close_date


# ── Golden: sector-weighted attribution inputs ────────────────────────────────────


def test_golden_attribution_input_sector_weights(db_session):
    """The market-value-weighted-by-sector breakdown ``compute_attribution`` consumes
    (``api/services/attribution.py::_portfolio_sector_aggregates``) — computed here
    directly from ``valued_holdings`` + the fixture's own sector assignments
    (``demo_household.SECURITY_META``), independent of the live attribution call
    (which additionally needs a benchmark-weights/backfill fetch out of scope for a
    fixture with no S3 artifact)."""
    demo_household.ensure_demo_household_seeded(db_session)
    valued = analytics.valued_holdings(db_session, demo_household.DEMO_TENANT_ID, demo_household.DEMO_HOUSEHOLD_PORTFOLIO_ID)
    priced = [h for h in valued if h.market_value]
    assert len(priced) == 25

    total_mv = sum(h.market_value for h in priced)
    assert total_mv == pytest.approx(242383.127166, rel=1e-6)

    by_sector: dict[str | None, float] = {}
    for h in priced:
        sector = demo_household.SECURITY_META[h.ticker][2]
        by_sector[sector] = by_sector.get(sector, 0.0) + h.market_value

    # The 3 funds (VTI/VOO/BND) carry no single GICS sector — excluded from a
    # sector-attribution weight, same as SECTOR_ETF-keyed lookups treat them.
    classified_mv = sum(v for k, v in by_sector.items() if k is not None)
    assert classified_mv < total_mv
    assert by_sector[None] == pytest.approx(total_mv - classified_mv)

    # All 8 GICS sectors used by the fixture are represented, each a non-trivial share
    # of the classified total (no sector silently empty).
    assert set(by_sector) - {None} == {
        "Technology", "Financial Services", "Healthcare", "Consumer Cyclical",
        "Consumer Defensive", "Energy", "Communication Services",
    }
    for sector, mv in by_sector.items():
        if sector is None:
            continue
        assert 0.0 < mv / classified_mv < 0.30, sector  # no single sector dominates


# ── Reserved DEMO- namespace: never touch a real tenant's shared Security/PriceBar ──
#
# ``securities`` and ``price_bars`` are GLOBAL, cross-tenant tables (see their
# docstrings in api/db/models.py) — a bare "AAPL" row here would be the SAME row a
# real tenant's real AAPL holding reads. Found in review of PR464 (a metron-ops#201-
# class defect) before merge; these three tests are the regression guard.


def test_fixture_symbols_are_all_namespaced(db_session):
    """Every symbol in BOTH committed fixture CSVs (and every ``SECURITY_META`` key)
    starts with ``DEMO-`` — a fixture-authoring regression here would otherwise write
    synthetic data straight into a real tenant's shared Security/PriceBar rows."""
    import csv as _csv

    tx_symbols = {
        row["symbol"]
        for row in _csv.DictReader(demo_household._load_transactions_csv().splitlines())
        if row["symbol"]
    }
    assert tx_symbols, "sanity: the transactions fixture must carry at least one security"
    for sym in tx_symbols:
        assert sym.startswith(demo_household.DEMO_SYMBOL_PREFIX), sym

    close_symbols = {
        sym for closes in demo_household._load_monthly_closes().values() for sym in closes
    }
    assert close_symbols, "sanity: the closes fixture must carry at least one symbol"
    for sym in close_symbols:
        assert sym.startswith(demo_household.DEMO_SYMBOL_PREFIX), sym

    for sym in demo_household.SECURITY_META:
        assert sym.startswith(demo_household.DEMO_SYMBOL_PREFIX), sym


def test_guard_raises_on_non_namespaced_price_bar_write(db_session):
    with pytest.raises(ValueError, match="DEMO-"):
        demo_household._seed_price_bars_for_date(db_session, date(2024, 1, 1), {"AAPL": 123.45})


def test_guard_raises_on_non_namespaced_security_meta(db_session, monkeypatch):
    monkeypatch.setitem(demo_household.SECURITY_META, "AAPL", ("Apple Inc.", "equity", "Technology"))
    try:
        with pytest.raises(ValueError, match="DEMO-"):
            demo_household._apply_security_meta(db_session)
    finally:
        del demo_household.SECURITY_META["AAPL"]


def test_seeding_never_touches_a_real_tenants_shared_security_or_price_bar(db_session):
    """Seed a real tenant's AAPL holding FIRST (its own Security row, priced on dates
    the demo household's own fixture also touches), then seed the demo household, and
    assert the real AAPL row — name/asset_class/sector and every one of its price
    bars — is byte-identical afterwards, with no bar added to it. This is the actual
    regression this namespace exists to prevent, exercised end to end through
    ``ensure_demo_household_seeded`` rather than only through the unit-level guards
    above."""
    real_tenant_id = __import__("uuid").uuid4()
    db_session.add(models.Tenant(id=real_tenant_id, name="Real Tenant"))
    real_security = models.Security(
        symbol="AAPL", name="Apple Inc.", currency="USD", asset_class="equity", sector="Technology"
    )
    db_session.add(real_security)
    db_session.commit()

    real_portfolio = models.Portfolio(tenant_id=real_tenant_id, name="Real Portfolio", base_currency="USD")
    db_session.add(real_portfolio)
    db_session.commit()
    real_account = models.Account(
        tenant_id=real_tenant_id, portfolio_id=real_portfolio.id, broker="manual", external_id="Real Brokerage"
    )
    db_session.add(real_account)
    db_session.commit()

    # Real price bars, on dates the demo household's own fixture ALSO writes a bar for
    # (the earliest and latest fixture month-end), plus one date the fixture doesn't
    # touch at all — every one of these must be untouched by seeding the household.
    fixture_dates = sorted(demo_household._load_monthly_closes())
    real_bar_dates = [fixture_dates[0], fixture_dates[-1], date(2020, 1, 1)]
    for d in real_bar_dates:
        db_session.add(models.PriceBar(security_id=real_security.id, bar_date=d, close=999.99, currency="USD"))
    db_session.commit()

    before_bars = {
        (b.bar_date, float(b.close))
        for b in db_session.scalars(
            select(models.PriceBar).where(models.PriceBar.security_id == real_security.id)
        ).all()
    }
    before_meta = (real_security.name, real_security.asset_class, real_security.sector)

    demo_household.ensure_demo_household_seeded(db_session)

    real_security_after = db_session.get(models.Security, real_security.id)
    after_bars = {
        (b.bar_date, float(b.close))
        for b in db_session.scalars(
            select(models.PriceBar).where(models.PriceBar.security_id == real_security.id)
        ).all()
    }
    after_meta = (real_security_after.name, real_security_after.asset_class, real_security_after.sector)

    assert after_meta == before_meta
    assert after_bars == before_bars  # no bar added, none altered

    # And the household's OWN "AAPL" holding lives under a completely separate
    # Security row (DEMO-AAPL), never the real one.
    demo_aapl = db_session.scalars(select(models.Security).where(models.Security.symbol == "DEMO-AAPL")).one()
    assert demo_aapl.id != real_security.id
