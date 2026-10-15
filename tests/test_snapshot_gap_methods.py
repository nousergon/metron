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
