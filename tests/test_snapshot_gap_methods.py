"""backfill-snapshot-gap opt-in methods (metron-ops-I361): restated anchors, trade-aware
repricing with flow conservation, carried closes, proxy bridges, the portfolio as the sum of
its account cohort, --only, --explain, and an undo that restores what was rebased/restated.

Every fixture is synthetic: made-up tickers, prices and ids."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from api import maintenance
from api.db import models
from api.services import snapshot_gap
from api.services.snapshot_gap import Options

A_DAY = date(2026, 10, 5)
GAP = (date(2026, 10, 6), date(2026, 10, 12))
B_DAY = date(2026, 10, 13)
GAP_DAYS = [GAP[0] + timedelta(days=i) for i in range(7)]
SESSIONS = [date(2026, 10, d) for d in (2, 5, 6, 7, 8, 9, 12, 13)]
FRI = date(2026, 10, 9)


def closes(base: float, step: float = 1.0, *, skip=()) -> dict[date, float]:
    return {d: base + step * d.day for d in SESSIONS if d not in skip}


AAA = closes(100.0)          # 105 on A, 113 on B
BBB = closes(200.0, 0.5)
SPY = closes(500.0)


def leg(ticker, qty, price, *, value=None, ccy="USD", fx=1.0, is_fund=False, stale=False, price_date=None):
    return {"ticker": ticker, "qty": qty, "price": price, "price_date": price_date, "currency": ccy,
            "fx_rate": fx, "value": value if value is not None else qty * price * fx,
            "is_fund": is_fund, "stale": stale, "proxy": None}


def asof(series: dict[date, float], d: date) -> float:
    return series[max(x for x in series if x <= d)]


class World:
    """One tenant, one portfolio, accounts, securities with close bars, snapshot rows."""

    def __init__(self, session, bars: dict[str, dict[date, float]] | None = None):
        self.s = session
        self.tenant = models.Tenant(name="t")
        session.add(self.tenant)
        session.flush()
        self.pf = self.portfolio()
        self.securities: dict[str, models.Security] = {}
        for sym, series in {"SPY": SPY, **(bars or {})}.items():
            self.bars(sym, series)

    def portfolio(self):
        pf = models.Portfolio(tenant_id=self.tenant.id, name="P", base_currency="USD")
        self.s.add(pf)
        self.s.flush()
        return pf

    def account(self, pf=None):
        self.n_accounts = getattr(self, "n_accounts", 0) + 1
        acct = models.Account(tenant_id=self.tenant.id, portfolio_id=(pf or self.pf).id, broker="csv",
                              external_id=f"X{self.n_accounts}", institution="Inst", currency="USD")
        self.s.add(acct)
        self.s.flush()
        return acct

    def bars(self, sym, series):
        sec = models.Security(symbol=sym, currency="USD", asset_class="equity")
        self.s.add(sec)
        self.s.flush()
        self.securities[sym] = sec
        self.s.add_all([models.PriceBar(security_id=sec.id, bar_date=d, close=c, currency="USD")
                        for d, c in series.items()])

    def snap(self, day, legs, *, account=None, pf=None, nav=None, flow=0.0, provisional=False, backfilled=False):
        model = models.AccountNavSnapshot if account is not None else models.NavSnapshot
        extra = {"account_id": account.id} if account is not None else {}
        comp = {"schema": 1, "legs": legs}
        if backfilled:
            comp["backfill"] = {"by": snapshot_gap.BACKFILL_MARKER, "at": "x"}
        row = model(tenant_id=self.tenant.id, portfolio_id=(pf or self.pf).id, snap_date=day,
                    nav=nav if nav is not None else sum(x["value"] for x in legs),
                    cost_basis=1000.0, external_flow=flow, provisional=provisional,
                    composition=comp, **extra)
        self.s.add(row)
        return row

    def txn(self, account, sym, kind, qty, amount, day):
        self.s.add(models.Transaction(
            tenant_id=self.tenant.id, account_id=account.id, security_id=self.securities[sym].id,
            txn_type=kind, quantity=qty, price=amount / qty if qty else 0, amount=amount,
            currency="USD", trade_date=day, source_key=f"{sym}-{kind}-{day}-{qty}"))

    def commit(self):
        self.s.commit()
        return self


def plan(session, **opts):
    return snapshot_gap.plan_gap(session, start=GAP[0], end=GAP[1], options=Options(**opts))


def one(plans, grain="account"):
    (p,) = [p for p in plans if p.grain == grain]
    return p


def rows(session, model=models.AccountNavSnapshot):
    return session.scalars(select(model).order_by(model.snap_date)).all()


# --- trade-aware ------------------------------------------------------------------------


def _bought_inside_gap(session, *, b_flow=540.0, amount=540.0, trade_day=date(2026, 10, 8)):
    w = World(session, {"AAA": AAA})
    acct = w.account()
    a = w.snap(A_DAY, [leg("AAA", 10, AAA[A_DAY])], account=acct)
    b = w.snap(B_DAY, [leg("AAA", 15, AAA[B_DAY])], account=acct, flow=b_flow)
    w.txn(acct, "AAA", "BUY", 5, amount, trade_day)
    w.commit()
    return w, acct, a, b


def test_trade_aware_values_each_day_at_that_days_holdings(db_session):
    _bought_inside_gap(db_session)
    assert one(plan(db_session)).skipped == "quantity of AAA changed inside the gap"  # default unchanged
    p = one(plan(db_session, trade_aware=True))
    assert p.skipped is None and p.method == "trade-aware" and not p.estimated
    assert p.points == {d: pytest.approx((10 if d < date(2026, 10, 8) else 15) * asof(AAA, d)) for d in GAP_DAYS}
    assert p.flows[date(2026, 10, 8)] == pytest.approx(540.0)
    assert sum(p.flows.values()) == pytest.approx(540.0)
    assert p.rebase is not None and p.rebase[1:] == (pytest.approx(540.0), pytest.approx(0.0))
    assert {"kind": "rebase-flow", "date": B_DAY.isoformat(),
            "external_flow_before": 540.0, "external_flow_after": 0.0} in p.updates()


def test_flow_conservation_holds_after_apply_and_undo_restores_the_rebased_flow(db_session):
    _w, _acct, _a, b = _bought_inside_gap(db_session)
    opts = Options(trade_aware=True)
    plans = snapshot_gap.plan_gap(db_session, start=GAP[0], end=GAP[1], options=opts)
    assert snapshot_gap.apply_plan(db_session, plans, options=opts) == 7
    written = rows(db_session)
    inserted = [r for r in written if GAP[0] <= r.snap_date <= GAP[1]]
    # The inserted flows plus B's rebased flow equal B's original flow, to the cent.
    assert sum(float(r.external_flow) for r in inserted) + float(b.external_flow) == pytest.approx(540.0, abs=0.005)
    assert b.composition["backfill_rebase"]["external_flow"] == 540.0
    meta = inserted[0].composition["backfill"]
    assert meta["method"] == "trade-aware" and meta["estimated"] is False and meta["estimated_legs"] == []

    assert snapshot_gap.restore_stashes(db_session, start=GAP[0], end=GAP[1], dry_run=True) == 1
    assert float(b.external_flow) == 0.0
    assert snapshot_gap.restore_stashes(db_session, start=GAP[0], end=GAP[1], dry_run=False) == 1
    assert snapshot_gap.undo(db_session, start=GAP[0], end=GAP[1], dry_run=False) == 7
    assert float(b.external_flow) == 540.0 and "backfill_rebase" not in b.composition
    assert [r.snap_date for r in rows(db_session)] == [A_DAY, B_DAY]


def test_flow_that_does_not_conserve_is_refused(db_session):
    _bought_inside_gap(db_session, b_flow=600.0)  # B's recorded flow disagrees with the ledger
    p = one(plan(db_session, trade_aware=True))
    assert p.skipped.startswith("flow conservation fails") and not p.points and p.rebase is None


def test_trade_day_return_check_catches_a_flowless_jump(db_session):
    _bought_inside_gap(db_session, b_flow=0.0, amount=0.0)
    p = one(plan(db_session, trade_aware=True))
    assert p.skipped.startswith("implausible") and "2026-10-08" in p.skipped


def test_trade_day_return_check_covers_a_trade_on_b(db_session):
    _bought_inside_gap(db_session, b_flow=0.0, amount=0.0, trade_day=B_DAY)
    p = one(plan(db_session, trade_aware=True))
    assert p.skipped.startswith("implausible") and B_DAY.isoformat() in p.skipped


def test_a_flow_before_another_writers_row_inside_the_gap_is_refused(db_session):
    w, acct, _a, _b = _bought_inside_gap(db_session, trade_day=date(2026, 10, 9))
    w.snap(date(2026, 10, 10), [leg("AAA", 15, AAA[FRI])], account=acct)
    w.commit()
    p = one(plan(db_session, trade_aware=True))
    assert p.skipped.startswith("a flow on 2026-10-09 precedes an existing row on 2026-10-10")


def _funded_from_cash_leg(session, *, lots=True, ledger_buy=False):
    """AAA bought with a money-market leg that has no close history (constant 1.0)."""
    w = World(session, {"AAA": AAA})
    acct = w.account()
    w.snap(A_DAY, [leg("AAA", 10, AAA[A_DAY]), leg("MMF1", 2000, 1.0)], account=acct)
    w.snap(B_DAY, [leg("AAA", 15, AAA[B_DAY]), leg("MMF1", 1460, 1.0)], account=acct,
           flow=540.0 if ledger_buy else 0.0)
    day = date(2026, 10, 8)
    if lots:
        session.add(models.OpenLot(tenant_id=w.tenant.id, account_id=acct.id, ticker="AAA", quantity=5,
                                   open_date=day, cost_basis=540.0, currency="USD", source="test"))
        session.add(models.RealizedLot(tenant_id=w.tenant.id, account_id=acct.id, ticker="MMF1",
                                       open_date=date(2026, 9, 1), close_date=day, quantity=540,
                                       proceeds=540.0, cost_basis=540.0, source="test", lot_key="k1"))
    if ledger_buy:
        w.txn(acct, "AAA", "BUY", 5, 540.0, day)  # the ledger knows the buy but not the funding sell
    w.commit()
    return w, acct


def test_trade_aware_uses_the_lots_when_the_ledger_does_not_reconcile_every_leg(db_session):
    _funded_from_cash_leg(db_session)
    p = one(plan(db_session, trade_aware=True))
    assert p.skipped is None, p.notes
    source = next(n for n in p.notes if n["kind"] == "trade-source")
    assert source["source"] == "lots"
    # Lot trades carry no ledger flow: B keeps exactly what it had, and the run says so.
    assert any(n["kind"] == "no-ledger-flow" for n in p.notes) and p.rebase is None
    assert not any(p.flows.values())
    assert p.points[date(2026, 10, 7)] == pytest.approx(10 * AAA[date(2026, 10, 7)] + 2000)
    assert p.points[date(2026, 10, 8)] == pytest.approx(15 * AAA[date(2026, 10, 8)] + 1460)


def test_trade_aware_refuses_when_no_source_reconciles_every_leg(db_session):
    _funded_from_cash_leg(db_session, lots=False, ledger_buy=True)
    p = one(plan(db_session, trade_aware=True))
    assert p.skipped == "no trade record (ledger or lots) reconciles A to B for every leg"
    tried = next(n for n in p.notes if n["kind"] == "trade-sources")["tried"]
    assert tried[0]["source"] == "ledger" and tried[0]["mismatches"][0]["ticker"] == "MMF1"


def test_trade_in_a_leg_held_at_neither_end_is_refused(db_session):
    w = World(db_session, {"AAA": AAA, "BBB": BBB})
    acct = w.account()
    w.snap(A_DAY, [leg("AAA", 10, AAA[A_DAY])], account=acct)
    w.snap(B_DAY, [leg("AAA", 15, AAA[B_DAY])], account=acct)
    w.txn(acct, "BBB", "BUY", 5, 500.0, date(2026, 10, 7))
    w.commit()
    p = one(plan(db_session, trade_aware=True))
    assert p.skipped and p.notes[0]["tried"][0]["mismatches"][0]["reason"] == "traded leg held at neither end"


def test_trade_aware_at_portfolio_grain_reads_every_account_of_the_portfolio(db_session):
    w = World(db_session, {"AAA": AAA})
    acct = w.account()
    w.snap(A_DAY, [leg("AAA", 10, AAA[A_DAY])])
    w.snap(B_DAY, [leg("AAA", 4, AAA[B_DAY])], flow=-6 * 108.0)
    w.txn(acct, "AAA", "SELL", 6, 6 * 108.0, date(2026, 10, 8))
    w.commit()
    p = one(plan(db_session, trade_aware=True), "portfolio")
    assert p.skipped is None
    assert p.points[date(2026, 10, 8)] == pytest.approx(4 * AAA[date(2026, 10, 8)])
    assert p.flows[date(2026, 10, 8)] == pytest.approx(-648.0)


# --- carry the previous close ---------------------------------------------------------


def test_missing_close_carry_flags_the_leg_day_and_prices_the_rest_from_their_own_close(db_session):
    no_bar = date(2026, 10, 8)
    w = World(db_session, {"AAA": closes(100.0, skip={no_bar}), "BBB": BBB})
    acct = w.account()
    w.snap(A_DAY, [leg("AAA", 10, AAA[A_DAY]), leg("BBB", 200, BBB[A_DAY])], account=acct)
    w.snap(B_DAY, [leg("AAA", 10, AAA[B_DAY]), leg("BBB", 200, BBB[B_DAY])], account=acct)
    w.commit()
    assert one(plan(db_session)).skipped == "no close for a held leg on 2026-10-08"  # default unchanged
    p = one(plan(db_session, missing_close="carry"))
    assert p.skipped is None and p.method == "reprice+carry" and p.estimated
    assert p.points[no_bar] == pytest.approx(10 * AAA[date(2026, 10, 7)] + 200 * BBB[no_bar])
    aaa = next(x for x in p.legs[no_bar] if x["ticker"] == "AAA")
    assert aaa["stale"] is True and aaa["price_date"] == "2026-10-07"
    assert [(e["ticker"], e["date"], e["how"]) for e in p.estimated_legs] == [("AAA", "2026-10-08", "carry")]
    assert all(not x["stale"] for d in GAP_DAYS if d != no_bar for x in p.legs[d])


def test_missing_close_carry_is_capped_at_five_percent_of_nav(db_session):
    w = World(db_session, {"AAA": closes(100.0, skip={date(2026, 10, 8)})})
    acct = w.account()
    w.snap(A_DAY, [leg("AAA", 10, AAA[A_DAY])], account=acct)
    w.snap(B_DAY, [leg("AAA", 10, AAA[B_DAY])], account=acct)
    w.commit()
    p = one(plan(db_session, missing_close="carry"))
    assert p.skipped == "carried legs exceed 5% of NAV on 2026-10-08" and not p.points


def test_missing_close_carry_carries_only_the_sessions_without_a_close(db_session):
    stop = {d for d in SESSIONS if d > date(2026, 10, 9)} - {B_DAY}
    w = World(db_session, {"AAA": closes(100.0, skip=stop), "BBB": BBB})
    acct = w.account()
    w.snap(A_DAY, [leg("AAA", 1, AAA[A_DAY]), leg("BBB", 200, BBB[A_DAY])], account=acct)
    w.snap(B_DAY, [leg("AAA", 1, AAA[B_DAY]), leg("BBB", 200, BBB[B_DAY])], account=acct)
    w.commit()
    p = one(plan(db_session, missing_close="carry"))
    assert p.skipped is None
    assert {e["date"] for e in p.estimated_legs} == {"2026-10-12"}


# --- restate the anchors --------------------------------------------------------------


def _stuck_provisional_anchor(session):
    fund = closes(40.0)  # struck 45 on A; the A row froze the 10-02 print (42)
    w = World(session, {"AAA": AAA, "FUND1": fund})
    acct = w.account()
    a = w.snap(A_DAY, [leg("AAA", 10, AAA[A_DAY]),
                       leg("FUND1", 100, fund[date(2026, 10, 2)], is_fund=True, stale=True, price_date="2026-10-02")],
               account=acct, provisional=True)
    w.snap(B_DAY, [leg("AAA", 10, AAA[B_DAY]),
                   leg("FUND1", 100, fund[B_DAY], is_fund=True, price_date=B_DAY.isoformat())], account=acct)
    w.commit()
    return a, fund


def test_restate_anchors_corrects_a_stuck_provisional_anchor_before_repricing(db_session):
    a, fund = _stuck_provisional_anchor(db_session)
    assert one(plan(db_session)).skipped.startswith("pricing method does not reproduce")  # default
    opts = Options(restate_anchors=True)
    plans = snapshot_gap.plan_gap(db_session, start=GAP[0], end=GAP[1], options=opts)
    p = one(plans)
    assert p.skipped is None and p.self_check == pytest.approx(0.0)
    assert p.updates() == [{"kind": "restate-anchor", "date": "2026-10-05", "nav_before": 1050.0 + 4200.0,
                            "nav_after": 1050.0 + 100 * fund[A_DAY], "still_provisional": False}]
    assert snapshot_gap.apply_plan(db_session, plans, options=opts) == 7
    assert float(a.nav) == pytest.approx(1050.0 + 100 * fund[A_DAY]) and a.provisional is False
    assert a.composition["backfill_restate"]["nav"] == pytest.approx(5250.0)

    assert snapshot_gap.restore_stashes(db_session, start=GAP[0], end=GAP[1], dry_run=False) == 1
    assert float(a.nav) == pytest.approx(5250.0) and a.provisional is True
    assert a.composition["legs"][1]["stale"] is True and "backfill_restate" not in a.composition


def test_cli_dry_run_with_restate_anchors_keeps_nothing(db_session, session_factory, monkeypatch, capsys):
    _stuck_provisional_anchor(db_session)
    _cli(monkeypatch, session_factory)
    assert maintenance.main([*CLI_GAP, "--restate-anchors"]) == 0
    out = capsys.readouterr().out
    assert '"updates": 1' in out and '"method": "reprice"' in out and '"would_write": 7' in out
    assert f'"nav_after": {1050.0 + 100 * 45.0}' in out and '"still_provisional": false' in out
    check = session_factory()
    a = check.scalars(select(models.AccountNavSnapshot).where(models.AccountNavSnapshot.snap_date == A_DAY)).one()
    assert float(a.nav) == pytest.approx(5250.0) and a.provisional is True
    assert len(check.scalars(select(models.AccountNavSnapshot)).all()) == 2
    check.close()


# --- proxy bridge ---------------------------------------------------------------------


def _broker_priced(session, *, priv_qty=10):
    idx = closes(300.0, 2.0)
    w = World(session, {"AAA": AAA, "IDX": idx})
    acct = w.account()
    w.snap(A_DAY, [leg("AAA", 100, AAA[A_DAY]), leg("PRIV1", priv_qty, 100.0)], account=acct)
    w.snap(B_DAY, [leg("AAA", 100, AAA[B_DAY]), leg("PRIV1", priv_qty, 104.0)], account=acct)
    w.commit()
    return idx


def test_proxy_bridge_chains_the_proxy_and_lands_on_both_recorded_prices(db_session):
    idx = _broker_priced(db_session)
    assert one(plan(db_session)).skipped.startswith("cannot re-price")  # default unchanged
    p = one(plan(db_session, proxy_bridge={"PRIV1": "IDX"}))
    assert p.skipped is None and p.self_check == pytest.approx(0.0) and p.method == "reprice+proxy-bridge"
    residual = (104.0 / 100.0) / (idx[B_DAY] / idx[A_DAY])
    sessions = [d for d in SESSIONS if A_DAY < d <= B_DAY]
    for d in GAP_DAYS:
        frac = sum(1 for s in sessions if s <= d) / len(sessions)
        want = 100.0 * (asof(idx, d) / idx[A_DAY]) * residual ** frac
        priv = next(x for x in p.legs[d] if x["ticker"] == "PRIV1")
        assert priv["price"] == pytest.approx(want) and priv["proxy"] == "IDX"
    # The bridge reaches B's recorded price exactly: frac = 1 on B.
    assert 100.0 * (idx[B_DAY] / idx[A_DAY]) * residual == pytest.approx(104.0)
    assert {e["how"] for e in p.estimated_legs} == {"proxy-bridge"} and len(p.estimated_legs) == 7


def test_proxy_bridge_is_capped_at_ten_percent_of_nav(db_session):
    _broker_priced(db_session, priv_qty=200)
    p = one(plan(db_session, proxy_bridge={"PRIV1": "IDX"}))
    assert p.skipped.startswith("proxy-bridged leg exceeds 10% of NAV")


def test_proxy_bridge_without_proxy_closes_is_refused(db_session):
    _broker_priced(db_session)
    p = one(plan(db_session, proxy_bridge={"PRIV1": "NOPE"}))
    assert p.skipped == "no close for a held leg on 2026-10-06"


# --- the portfolio as the sum of its account cohort -----------------------------------


def _household(session, *, second_account_gapped=True):
    """Two accounts; the first already has every gap day, the second is gapped but plannable.
    The portfolio has its ends plus one other writer's row inside the gap (10-08)."""
    w = World(session, {"AAA": AAA, "BBB": BBB})
    one_, two = w.account(), w.account()
    w.snap(A_DAY, [leg("AAA", 10, AAA[A_DAY])], account=one_)
    for d in GAP_DAYS:
        w.snap(d, [leg("AAA", 10, asof(AAA, d))], account=one_, backfilled=True)
    w.snap(B_DAY, [leg("AAA", 10, AAA[B_DAY])], account=one_)
    w.snap(A_DAY, [leg("BBB", 20, BBB[A_DAY])], account=two)
    w.snap(B_DAY, [leg("BBB", 20, BBB[B_DAY])], account=two)
    if not second_account_gapped:
        for d in GAP_DAYS:
            w.snap(d, [leg("BBB", 20, asof(BBB, d))], account=two)
    for d in (A_DAY, B_DAY):
        w.snap(d, [leg("AAA", 10, AAA[d]), leg("BBB", 20, BBB[d])])
    w.snap(date(2026, 10, 8), [leg("AAA", 10, 1.0)], nav=3333.0)  # a reconstruct-owned row
    # Another portfolio's account, fully present: never part of this cohort.
    other_pf = w.portfolio()
    stranger = w.account(other_pf)
    for d in (A_DAY, *GAP_DAYS, B_DAY):
        w.snap(d, [leg("AAA", 1000, asof(AAA, d))], account=stranger, pf=other_pf)
    w.commit()
    return w, one_, two


def _sum(d):
    return 10 * asof(AAA, d) + 20 * asof(BBB, d)


def test_portfolio_from_accounts_sums_every_account_on_the_missing_days_only(db_session):
    _household(db_session)
    opts = Options(portfolio_from_accounts=True, restate_anchors=True)  # nothing provisional to restate
    plans = snapshot_gap.plan_gap(db_session, start=GAP[0], end=GAP[1], options=opts)
    p = one(plans, "portfolio")
    assert all(not x.restated for x in plans)
    assert p.skipped is None and p.method == "sum-of-accounts" and p.self_check == pytest.approx(0.0)
    assert p.points == {d: pytest.approx(_sum(d)) for d in GAP_DAYS if d != date(2026, 10, 8)}
    assert len(p.legs[GAP[0]]) == 2
    note = next(n for n in p.notes if n["kind"] == "existing-row-vs-account-sum")
    assert note["date"] == "2026-10-08" and note["recorded"] == 3333.0
    assert snapshot_gap.apply_plan(db_session, plans, options=opts) == 7 + 6
    navs = {r.snap_date: float(r.nav) for r in rows(db_session, models.NavSnapshot)
            if r.portfolio_id == p.portfolio_id}
    assert navs[date(2026, 10, 8)] == 3333.0  # left to its own writer


def test_portfolio_from_accounts_is_restricted_to_the_cohort(db_session):
    _w, one_, two = _household(db_session)
    # --only the portfolio: the gapped account is not planned, so the cohort is incomplete.
    p = one(plan(db_session, portfolio_from_accounts=True, only=frozenset({"portfolio"})), "portfolio")
    assert p.skipped == "not every account of the cohort has a row on 2026-10-06" and not p.points
    cohort = next(n for n in p.notes if n["kind"] == "cohort")
    assert cohort["lacking"] == [str(two.id)] and cohort["outside"] == []


def test_portfolio_from_accounts_refuses_a_cohort_that_changes_or_does_not_sum(db_session):
    w, one_, _two = _household(db_session, second_account_gapped=False)
    a_row = db_session.scalars(select(models.AccountNavSnapshot).where(
        models.AccountNavSnapshot.account_id == one_.id, models.AccountNavSnapshot.snap_date == A_DAY)).one()
    a_row.nav = 1.0
    db_session.commit()
    p = one(plan(db_session, portfolio_from_accounts=True), "portfolio")
    assert p.skipped == "the accounts do not sum to the portfolio on 2026-10-05"
    db_session.delete(a_row)
    db_session.commit()
    p = one(plan(db_session, portfolio_from_accounts=True), "portfolio")
    assert p.skipped == "the account cohort differs between the two ends of the gap"


def test_only_restricts_the_run_to_the_named_series(db_session):
    _w, _one, two = _household(db_session)
    plans = plan(db_session, only=frozenset({str(two.id)}))
    assert [(p.grain, p.account_id) for p in plans] == [("account", two.id)]


# --- explain --------------------------------------------------------------------------


def test_explain_lists_every_failing_leg_with_its_cause(db_session):
    w = World(db_session, {"AAA": closes(100.0, skip={date(2026, 10, 8)}),
                           "DDD": closes(100.0, skip={d for d in SESSIONS if d > A_DAY}), "EEE": closes(10.0)})
    acct = w.account()
    w.snap(A_DAY, [leg("AAA", 10, AAA[A_DAY]), leg("PRIV1", 1, 100.0), leg("DDD", 1, 105.0),
                   leg("EEE", 1, 15.0, ccy="EUR"), leg("FFF", 1, None, value=5.0)], account=acct, provisional=True)
    w.snap(B_DAY, [leg("AAA", 12, AAA[B_DAY]), leg("PRIV1", 1, 104.0), leg("DDD", 1, 105.0),
                   leg("EEE", 1, 23.0, ccy="EUR"), leg("FFF", 1, None, value=5.0)], account=acct)
    w.commit()
    p = one(plan(db_session, explain=True))
    assert p.skipped == "quantity of AAA changed inside the gap"
    ex = p.explain
    assert ex["a_provisional"] is True and ex["b_provisional"] is False and ex["a_date"] == "2026-10-05"
    causes = {(x["ticker"], x["cause"]) for x in ex["legs"]}
    assert {("AAA", "qty-changed"), ("FFF", 1), ("PRIV1", 2), ("DDD", 3), ("EEE", 5), ("AAA", 4)} <= causes
    priv = next(x for x in ex["legs"] if x["ticker"] == "PRIV1")
    assert priv["a_price"] == 100.0 and priv["b_price"] == 104.0 and 0 < priv["weight"] < 1
    assert one(plan(db_session)).explain is None


# --- CLI ------------------------------------------------------------------------------


CLI_GAP = ["backfill-snapshot-gap", "--from", "2026-10-06", "--through", "2026-10-12"]


def _cli(monkeypatch, session_factory):
    monkeypatch.setattr(maintenance, "session_today", lambda: date(2026, 10, 14))
    monkeypatch.setattr(maintenance, "SessionLocal", session_factory)
    monkeypatch.setattr(maintenance, "create_all", lambda: None)


def test_cli_parses_every_option_into_options(monkeypatch):
    seen = {}
    monkeypatch.setattr(maintenance, "_backfill_snapshot_gap", lambda s, e, **kw: seen.update(kw) or 0)
    assert maintenance.main([*CLI_GAP, "--explain", "--restate-anchors", "--trade-aware", "--missing-close",
                             "carry", "--proxy-bridge", "PRIV1=IDX", "--portfolio-from-accounts",
                             "--only", "portfolio", "--only", "abc"]) == 0
    assert seen["options"] == Options(explain=True, restate_anchors=True, trade_aware=True, missing_close="carry",
                                      proxy_bridge={"PRIV1": "IDX"}, portfolio_from_accounts=True,
                                      only=frozenset({"portfolio", "abc"}))
    maintenance.main(CLI_GAP)
    assert seen["options"] == Options() and not seen["options"].extended


def test_cli_rejects_a_malformed_bridge_and_unsafe_combinations(monkeypatch):
    with pytest.raises(SystemExit):
        maintenance.main([*CLI_GAP, "--proxy-bridge", "PRIV1"])
    monkeypatch.setattr(maintenance, "session_today", lambda: date(2026, 10, 14))
    assert maintenance.main([*CLI_GAP, "--explain", "--apply"]) == 2
    assert maintenance.main([*CLI_GAP, "--undo", "--trade-aware"]) == 2


def test_cli_trade_aware_apply_then_undo_restores_the_flow(db_session, session_factory, monkeypatch, capsys):
    _w, _acct, _a, b = _bought_inside_gap(db_session)
    _cli(monkeypatch, session_factory)
    assert maintenance.main([*CLI_GAP, "--trade-aware", "--apply"]) == 0
    out = capsys.readouterr().out
    assert '"written": 7' in out and '"updates": 1' in out and '"method": "trade-aware"' in out
    assert maintenance.main([*CLI_GAP, "--undo", "--apply"]) == 0
    assert capsys.readouterr().out.strip() == '{"mode": "undo", "rows": 7, "restored": 1}'
    db_session.expire_all()
    assert float(db_session.get(models.AccountNavSnapshot, b.id).external_flow) == 540.0


def test_cli_default_output_is_unchanged(db_session, session_factory, monkeypatch, capsys):
    _household(db_session)
    _cli(monkeypatch, session_factory)
    assert maintenance.main(CLI_GAP) == 0
    out = capsys.readouterr().out
    assert '"updates"' not in out and '"method"' not in out and '"explain"' not in out
    assert maintenance.main([*CLI_GAP, "--undo"]) == 0
    assert capsys.readouterr().out.strip() == '{"mode": "undo-dry-run", "rows": 7}'
    assert maintenance.main([*CLI_GAP, "--apply"]) == 0
    assert all(set(r.composition["backfill"]) == {"by", "at"}
               for r in rows(session_factory(), models.NavSnapshot) if "backfill" in (r.composition or {}))


# --- trade-aware: splits ---------------------------------------------------------------

SPLIT_DAY = date(2026, 10, 8)
CCC_RAW = {d: (200.0 + d.day) / (5 if d >= SPLIT_DAY else 1) for d in SESSIONS}   # unadjusted, steps 1/5
CCC_ADJ = {d: (200.0 + d.day) / 5 for d in SESSIONS}                              # back-adjusted


def _split_world(session, stored, *, b_qty=50, recorded=None, extra=()):
    w = World(session, {"CCC": stored, **dict(extra)})
    acct = w.account()
    raw_a, raw_b = 200.0 + A_DAY.day, (200.0 + B_DAY.day) / 5  # what the live writer recorded
    w.snap(A_DAY, [leg("CCC", 10, raw_a)], account=acct)
    w.snap(B_DAY, [leg("CCC", b_qty, raw_b)], account=acct)
    if recorded is not None:
        w.txn(acct, "CCC", "SPLIT", recorded[1], 0.0, recorded[0])
    w.commit()
    return w, acct


def _held(d):  # what the account actually held, at each day's raw close
    return (10 if d < SPLIT_DAY else 50) * asof(CCC_RAW, d)


def test_split_on_an_unadjusted_series_steps_on_the_jump_day_with_no_flow(db_session):
    _split_world(db_session, CCC_RAW)
    assert one(plan(db_session)).skipped == "quantity of CCC changed inside the gap"  # default unchanged
    p = one(plan(db_session, trade_aware=True))
    assert p.skipped is None and p.method == "trade-aware+split" and not p.estimated
    assert p.points == {d: pytest.approx(_held(d)) for d in GAP_DAYS}
    assert [x["qty"] for x in p.legs[date(2026, 10, 7)]] == [10] and [x["qty"] for x in p.legs[SPLIT_DAY]] == [50]
    assert not any(p.flows.values()) and p.rebase is None
    note = next(n for n in p.notes if n["kind"] == "split")
    assert note == {"kind": "split", "ticker": "CCC", "ratio": 5.0, "terms": "5:1", "a_qty": 10, "b_qty": 50,
                    "recorded_date": None, "date": "2026-10-08", "basis": "unadjusted"}


def test_split_on_a_split_adjusted_series_values_the_same_navs(db_session):
    _split_world(db_session, CCC_ADJ)
    p = one(plan(db_session, trade_aware=True))
    assert p.skipped is None and p.method == "trade-aware+split" and p.self_check == pytest.approx(0.0)
    # b_qty × the adjusted close IS a_qty × the raw close before the split: same NAV every day.
    assert p.points == {d: pytest.approx(_held(d)) for d in GAP_DAYS}
    assert all(x["qty"] == 50 for d in GAP_DAYS for x in p.legs[d])
    note = next(n for n in p.notes if n["kind"] == "split")
    assert note["basis"] == "adjusted" and note["date"] is None and note["ratio"] == 5.0


def test_a_recorded_split_supplies_the_date_and_must_agree_on_the_ratio(db_session):
    w, _acct = _split_world(db_session, CCC_ADJ, recorded=(SPLIT_DAY, 5.0))
    note = next(n for n in one(plan(db_session, trade_aware=True)).notes if n["kind"] == "split")
    assert note["date"] == note["recorded_date"] == "2026-10-08" and note["basis"] == "adjusted"
    db_session.query(models.Transaction).update({"quantity": 2.0})
    db_session.commit()
    p = one(plan(db_session, trade_aware=True))
    assert p.skipped == "no trade record (ledger or lots) reconciles A to B for every leg"
    miss = p.notes[0]["tried"][0]["mismatches"][0]
    assert miss["not_split"] == "recorded split ratio 2 does not match the quantity ratio 5:1"


@pytest.mark.parametrize("stored, b_qty, why", [
    # No jump anywhere and A's stored close is A's recorded price: nothing proves a split.
    ({d: 200.0 + d.day for d in SESSIONS}, 50, "no close-to-close step of 1/5:1 inside the gap"),
    # A one-day step that is not 1/5 (a -10% day) is not a 5:1 split.
    ({d: (200.0 + d.day) * (0.9 if d >= SPLIT_DAY else 1) for d in SESSIONS}, 50, "no close-to-close step"),
    # Two days both step by 1/2: the date is ambiguous.
    ({d: (200.0 + d.day) / (4 if d >= SPLIT_DAY else 2 if d >= date(2026, 10, 7) else 1) for d in SESSIONS}, 20,
     "2 sessions step by 1/2:1"),
    (CCC_RAW, 47.7, "quantity ratio 4.77 is not a simple split ratio"),
    (CCC_RAW, 11, "quantity ratio 1.1 is too small to tell from a day's price move"),
])
def test_a_split_is_never_invented_without_matching_price_evidence(db_session, stored, b_qty, why):
    _split_world(db_session, stored, b_qty=b_qty)
    p = one(plan(db_session, trade_aware=True))
    assert p.skipped == "no trade record (ledger or lots) reconciles A to B for every leg" and not p.points
    (tried,) = next(n for n in p.notes if n["kind"] == "trade-sources")["tried"]
    assert tried["source"] == "none" and tried["mismatches"][0]["not_split"].startswith(why)


def test_a_split_without_close_history_is_refused(db_session):
    w = World(db_session)
    acct = w.account()
    w.snap(A_DAY, [leg("PRIV1", 10, 100.0)], account=acct)
    w.snap(B_DAY, [leg("PRIV1", 50, 20.0)], account=acct)
    w.commit()
    miss = one(plan(db_session, trade_aware=True)).notes[0]["tried"][0]["mismatches"][0]
    assert miss["not_split"].startswith("no close history") and miss["not_drip"].startswith("increase of 400.00%")
    assert _split_reason(0, 1) == "not held at both ends"


def _split_reason(qa, qb):
    return snapshot_gap._split(("X", "USD"), qa, qb, None, None, {}, {}, None)


def test_a_split_next_to_lot_trades_reconciles_from_the_same_source(db_session):
    w, acct = _funded_from_cash_leg(db_session)  # AAA bought from cash, on the lots only
    w.bars("CCC", CCC_RAW)
    for row in rows(db_session):
        raw = CCC_RAW[row.snap_date]
        qty = 10 if row.snap_date == A_DAY else 50
        row.composition = {**row.composition, "legs": [*row.composition["legs"], leg("CCC", qty, raw)]}
        row.nav = float(row.nav) + qty * raw
    w.commit()
    p = one(plan(db_session, trade_aware=True))
    assert p.skipped is None and p.method == "trade-aware+split"
    assert next(n for n in p.notes if n["kind"] == "trade-source")["source"] == "lots"
    d = date(2026, 10, 7)
    assert p.points[d] == pytest.approx(10 * AAA[d] + 2000 + 10 * CCC_RAW[d])
    assert p.points[SPLIT_DAY] == pytest.approx(15 * AAA[SPLIT_DAY] + 1460 + 50 * CCC_RAW[SPLIT_DAY])


# --- trade-aware: dividend reinvestment ------------------------------------------------


def _drip_world(session, b_qty=20.002, dividend_on=None):
    w = World(session, {"AAA": AAA, "BBB": BBB})
    acct = w.account()
    w.snap(A_DAY, [leg("AAA", 20.0, AAA[A_DAY]), leg("BBB", 5, BBB[A_DAY])], account=acct)
    w.snap(B_DAY, [leg("AAA", b_qty, AAA[B_DAY]), leg("BBB", 5, BBB[B_DAY])], account=acct)
    if dividend_on:
        w.txn(acct, "AAA", "DIVIDEND", 0, 1.2, dividend_on)
    w.commit()
    return w


def test_drip_without_a_recorded_dividend_steps_on_the_first_day_after_a(db_session):
    _drip_world(db_session)
    p = one(plan(db_session, trade_aware=True))
    assert p.skipped is None and p.method == "trade-aware+drip"
    assert p.points == {d: pytest.approx(20.002 * asof(AAA, d) + 5 * asof(BBB, d)) for d in GAP_DAYS}
    assert not any(p.flows.values()) and p.rebase is None  # the dividend is return, not a flow
    note = next(n for n in p.notes if n["kind"] == "drip")
    assert note["date"] == "2026-10-06" and note["date_source"] == "first-day-after-A"
    assert note["fraction"] == pytest.approx(0.002 / 20.0)


def test_drip_steps_on_the_recorded_dividend(db_session):
    _drip_world(db_session, dividend_on=FRI)
    p = one(plan(db_session, trade_aware=True))
    for d in GAP_DAYS:
        assert p.points[d] == pytest.approx((20.0 if d < FRI else 20.002) * asof(AAA, d) + 5 * asof(BBB, d))
    note = next(n for n in p.notes if n["kind"] == "drip")
    assert note["date"] == FRI.isoformat() and note["date_source"] == "ledger-dividend"


@pytest.mark.parametrize("b_qty, why", [(20.5, "increase of 2.50% exceeds"), (19.9, "not an increase")])
def test_drip_is_bounded_to_a_small_increase(db_session, b_qty, why):
    _drip_world(db_session, b_qty=b_qty)
    p = one(plan(db_session, trade_aware=True))
    assert p.skipped and p.notes[0]["tried"][0]["mismatches"][0]["not_drip"].startswith(why)


# --- proportional anchoring ------------------------------------------------------------


def _off_by(session, b_error):
    w = World(session, {"AAA": AAA})
    acct = w.account()
    w.snap(A_DAY, [leg("AAA", 10, AAA[A_DAY])], account=acct)
    w.snap(B_DAY, [leg("AAA", 10, AAA[B_DAY])], account=acct, nav=10 * AAA[B_DAY] * (1 + b_error))
    w.commit()


def test_proportional_anchor_blends_the_end_scales_linearly_in_time(db_session):
    _off_by(db_session, 0.01)
    assert one(plan(db_session)).skipped.startswith("pricing method does not reproduce")  # default unchanged
    opts = Options(anchor="proportional")
    plans = snapshot_gap.plan_gap(db_session, start=GAP[0], end=GAP[1], options=opts)
    p = one(plans)
    assert p.skipped is None and p.method == "reprice+anchor" and p.estimated
    assert p.self_check == pytest.approx(0.01 / 1.01)
    for d in GAP_DAYS:
        f = 1 + 0.01 * (d - A_DAY).days / (B_DAY - A_DAY).days
        assert p.points[d] == pytest.approx(10 * asof(AAA, d) * f)
        assert p.legs[d][0]["anchor_scale"] == pytest.approx(f)
    note = next(n for n in p.notes if n["kind"] == "anchor")
    assert note["scale_a"] == pytest.approx(1.0) and note["scale_b"] == pytest.approx(1.01)
    assert snapshot_gap.apply_plan(db_session, plans, options=opts) == 7
    meta = rows(db_session)[1].composition["backfill"]
    assert meta["method"] == "reprice+anchor" and meta["estimated"] is True
    assert meta["estimated_legs"][0]["how"] == "anchor"


def test_proportional_anchor_refuses_an_end_off_by_more_than_two_percent(db_session):
    _off_by(db_session, 0.03)
    p = one(plan(db_session, anchor="proportional"))
    assert p.skipped.endswith("(and an end is off by more than the 2% anchoring bound)") and not p.points


# --- operator-supplied leg prices ------------------------------------------------------


def _priv_prices(skip=()):
    return {d: 100.0 + 0.5 * i for i, d in enumerate(SESSIONS) if d not in skip}


def test_leg_prices_price_a_broker_priced_leg_from_the_operator_file(db_session):
    _broker_priced(db_session)
    supplied = _priv_prices()
    p = one(plan(db_session, leg_prices={"PRIV1": supplied}))
    assert p.skipped is None and p.method == "reprice+operator-price" and p.estimated
    for d in GAP_DAYS:
        assert p.points[d] == pytest.approx(100 * asof(AAA, d) + 10 * asof(supplied, d))
    assert {e["ticker"] for e in p.estimated_legs} == {"PRIV1"} and len(p.estimated_legs) == 7


def test_leg_prices_fall_back_to_the_recorded_price_at_the_ends_and_before_any_supplied_day(db_session):
    _broker_priced(db_session)
    supplied = {d: 101.0 for d in SESSIONS if GAP[0] <= d <= GAP[1]}
    p = one(plan(db_session, leg_prices={"PRIV1": supplied}))
    # The ends had no supplied price and kept the recorded 100 / 104: the self-check is exact.
    assert p.skipped is None and p.self_check == pytest.approx(0.0)
    assert p.points[GAP[0]] == pytest.approx(100 * AAA[GAP[0]] + 10 * 101.0)


def test_leg_prices_missing_a_session_fails_loudly(db_session):
    _broker_priced(db_session)
    with pytest.raises(snapshot_gap.LegPricesError, match="no price for PRIV1 on 2026-10-08"):
        plan(db_session, leg_prices={"PRIV1": _priv_prices(skip={date(2026, 10, 8)})})
    with pytest.raises(snapshot_gap.LegPricesError, match="no gapped series holds: NOPE"):
        plan(db_session, leg_prices={"PRIV1": _priv_prices(), "NOPE": {A_DAY: 1.0}})


def test_operator_price_guards_inside_the_valuer(db_session):
    _broker_priced(db_session)
    a, b = rows(db_session)
    a_agg = snapshot_gap._aggregate(a.composition["legs"])
    b_agg = snapshot_gap._aggregate(b.composition["legs"])
    hist = {"SPY": [snapshot_gap.price_service.ClosePoint(bar_date=d, close=1.0) for d in SESSIONS]}
    opts = Options(leg_prices={"PRIV1": {date(2026, 10, 9): 103.0}, "GONE": {}})
    v = snapshot_gap._Valuer(a, b, a_agg, b_agg, b_agg, hist, None, opts)
    k = ("PRIV1", "USD")
    assert v._operator_price(k, date(2026, 10, 10), None) == (103.0, date(2026, 10, 9), "operator-price")
    assert v._operator_price(k, date(2026, 10, 4), None) == (100.0, None, "operator-price")  # before any
    assert v._operator_price(k, A_DAY, "A") == (100.0, None, None)
    assert v._operator_price(("GONE", "USD"), A_DAY, "A") == 2
    assert v._operator_price(("GONE", "USD"), date(2026, 10, 11), None) == 2
    with pytest.raises(snapshot_gap.LegPricesError):
        v._operator_price(k, date(2026, 10, 8), None)


# --- explain: account sums and the per-leg reprice residual -----------------------------


def test_explain_shows_how_far_the_accounts_are_from_the_portfolio(db_session):
    _w, one_, _two = _household(db_session, second_account_gapped=False)
    a_row = db_session.scalars(select(models.AccountNavSnapshot).where(
        models.AccountNavSnapshot.account_id == one_.id, models.AccountNavSnapshot.snap_date == B_DAY)).one()
    a_row.nav = float(a_row.nav) - 50.0
    db_session.commit()
    p = one(plan(db_session, portfolio_from_accounts=True, explain=True), "portfolio")
    assert p.skipped == "the accounts do not sum to the portfolio on 2026-10-13"
    at_a, at_b = p.explain["account_sum"]
    assert at_a["diff"] == 0.0 and at_a["rel_diff_pct"] == 0.0 and len(at_a["accounts"]) == 2
    assert at_b["date"] == "2026-10-13" and at_b["diff"] == -50.0
    assert at_b["rel_diff_pct"] == pytest.approx(round(-50.0 / _sum(B_DAY) * 100, 4))
    # Without --explain nothing extra is computed or reported.
    assert one(plan(db_session, portfolio_from_accounts=True), "portfolio").explain is None


def test_explain_locates_a_self_check_residual_leg_by_leg(db_session):
    _stuck_provisional_anchor(db_session)
    p = one(plan(db_session, explain=True))
    assert p.skipped.startswith("pricing method does not reproduce")
    at_a, at_b = p.explain["reprice"]
    assert at_a["anchor"] == "A" and at_a["recorded_nav"] == 5250.0
    worst = at_a["legs"][0]
    assert worst["ticker"] == "FUND1" and worst["recorded_value"] == 4200.0
    assert worst["repriced_value"] == pytest.approx(4500.0) and worst["diff"] == pytest.approx(300.0)
    assert worst["diff_pct_of_nav"] == pytest.approx(round(300 / 5250 * 100, 4))
    assert at_a["rel_error_pct"] == pytest.approx(round(300 / 5250 * 100, 4))
    assert at_b["rel_error_pct"] == 0.0 and all(x["diff"] == 0.0 for x in at_b["legs"])


def test_explain_reprice_detail_reports_a_leg_it_cannot_price(db_session):
    _broker_priced(db_session)
    a, _b = rows(db_session)
    agg = snapshot_gap._aggregate(a.composition["legs"])
    hist = {"AAA": [snapshot_gap.price_service.ClosePoint(bar_date=d, close=c) for d, c in AAA.items()]}

    class NoFx:
        @staticmethod
        def rate_as_of(_ccy, _d):
            return None

    v = snapshot_gap._Valuer(a, _b, agg, agg, agg, hist, NoFx, Options())
    out = snapshot_gap._reprice_detail(a, "A", agg, v)
    assert out["repriced_nav"] is None and out["rel_error_pct"] is None
    assert all(x["repriced_value"] is None and x["diff"] is None for x in out["legs"])


# --- CLI: the new options --------------------------------------------------------------


def test_cli_parses_anchor_and_leg_prices(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(maintenance, "_backfill_snapshot_gap", lambda s, e, **kw: seen.update(kw) or 0)
    f = tmp_path / "p.csv"
    f.write_text("ticker,date,price\nPRIV1,2026-10-06,101.5\n\nPRIV1,2026-10-07, 102\n")
    assert maintenance.main([*CLI_GAP, "--anchor", "proportional", "--leg-prices", str(f)]) == 0
    assert seen["options"] == Options(anchor="proportional",
                                      leg_prices={"PRIV1": {date(2026, 10, 6): 101.5, date(2026, 10, 7): 102.0}})


@pytest.mark.parametrize("body", ["PRIV1,2026-10-06\n", "PRIV1,not-a-date,1\n", "PRIV1,2026-10-06,-1\n",
                                  "PRIV1,2026-10-06,1\nPRIV1,2026-10-06,2\n", "ticker,date,price\n", None])
def test_cli_rejects_a_malformed_leg_prices_file(tmp_path, body):
    f = tmp_path / "p.csv"
    if body is not None:
        f.write_text(body)
    with pytest.raises(SystemExit):
        maintenance.main([*CLI_GAP, "--leg-prices", str(f)])


def test_cli_leg_prices_missing_a_day_exits_two(db_session, session_factory, monkeypatch, tmp_path):
    _broker_priced(db_session)
    _cli(monkeypatch, session_factory)
    f = tmp_path / "p.csv"
    f.write_text("PRIV1,2026-10-06,101\n")
    assert maintenance.main([*CLI_GAP, "--leg-prices", str(f)]) == 2


def test_a_traded_leg_that_does_not_reconcile_is_never_explained_as_a_split_or_drip(db_session):
    _bought_inside_gap(db_session)
    db_session.query(models.AccountNavSnapshot).filter_by(snap_date=B_DAY).one().composition = {
        "schema": 1, "legs": [leg("AAA", 75, AAA[B_DAY])]}  # 5 bought, yet 10 -> 75
    db_session.commit()
    miss = one(plan(db_session, trade_aware=True)).notes[0]["tried"][0]["mismatches"][0]
    assert miss == {"ticker": "AAA", "a_qty": 10, "traded": 5.0, "b_qty": 75}
