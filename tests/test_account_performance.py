"""Golden examples for the account-basis performance engine (metron-ops#371, slice 1).

Each fixture is a synthetic statement worked by hand: the end wealth and every
period's P&L components are written out below and must reconcile to the cent. The
FX fixture (design §6 item 7) waits on metron-ops#370's as-of FX and is not here.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from api.services.performance import _purchase_flow
from portfolio_analytics.domain import account_performance as ap
from portfolio_analytics.domain.account_performance import AccountEvent, EventKind, WealthObservation
from portfolio_analytics.domain.ledger import Transaction, TxnType

D0 = date(2026, 1, 5)


def d(i: int) -> date:
    return D0 + timedelta(days=i)


def obs(i: int, mv: float, cash: float | None, *, accrued: float | None = 0.0,
        debit: float | None = 0.0, source: str = "custodian") -> WealthObservation:
    return WealthObservation(d(i), mv, cash, source, accrued, debit)


def cents(x: float | None) -> float:
    assert x is not None
    return round(x, 2)


# ── the golden statements ────────────────────────────────────────────────────


def test_golden_1_idle_cash_dilutes_the_account_return():
    # Statement: holdings 1,000.00 -> 1,100.00, cash 500.00 idle throughout.
    perf = ap.account_performance([obs(0, 1000, 500), obs(1, 1100, 500)], [])
    (p,) = perf.periods
    assert (cents(p.wealth_start), cents(p.wealth_end)) == (1500.00, 1600.00)
    assert (cents(p.price_pnl), cents(p.income), cents(p.explicit_costs)) == (100.00, 0.00, 0.00)
    assert p.status == "reconciled" and p.residual == 0
    # The sleeve would say +10%; the account earned 100 on 1,500.
    assert perf.metrics["twr_net"].value == pytest.approx(100 / 1500)


def test_golden_2_dividend_paid_to_cash_is_income_not_capital():
    # Statement: 20.00 dividend lands in cash; holdings flat at 1,000.00.
    events = [AccountEvent(d(1), EventKind.DIVIDEND, 20.0)]
    perf = ap.account_performance([obs(0, 1000, 0), obs(1, 1000, 20)], events)
    (p,) = perf.periods
    assert cents(p.wealth_end) == 1020.00
    assert (p.external, cents(p.income), cents(p.price_pnl)) == (0.0, 20.00, 0.00)
    assert cents(p.net_pnl) == 20.00
    assert perf.metrics["twr_net"].value == pytest.approx(0.02)


def test_golden_3_reinvested_dividend_is_never_external_capital_on_the_account_basis():
    # Statement: 20.00 dividend reinvested as 2 shares at 10.00; prices flat.
    txns = [
        Transaction(d(1), TxnType.DIVIDEND, "ABC", amount=20.0),
        Transaction(d(1), TxnType.REINVESTMENT, "ABC", quantity=2, price=10.0),
    ]
    events = [ap.from_transaction(t) for t in txns]
    perf = ap.account_performance([obs(0, 1000, 0), obs(1, 1020, 0)], events)
    (p,) = perf.periods
    assert p.external == 0.0
    assert (cents(p.income), cents(p.price_pnl), cents(p.net_pnl)) == (20.00, 0.00, 20.00)
    assert all(ap.account_external_flow(e) == 0.0 for e in events)
    # The holdings sleeve counts that same buy as capital in. The two bases disagree on
    # external flows by design (metron-ops#371 §6, fixture 3).
    sleeve_flow = _purchase_flow([(t.type.value, t.quantity * t.price or t.amount) for t in txns])
    assert sleeve_flow == 20.0 != p.external


def test_golden_4_commission_and_fee_separate_gross_from_net():
    # Statement: cash 1,000.00; buy 10 @ 50.00 with 1.00 commission; 5.00 account fee;
    # close at 51.00. Cash 1,000 - 501 - 5 = 494.00; holdings 510.00.
    events = [
        AccountEvent(d(1), EventKind.BUY, 500.0, fees=1.0),
        AccountEvent(d(1), EventKind.FEE, 5.0),
    ]
    perf = ap.account_performance([obs(0, 0, 1000), obs(1, 510, 494)], events)
    (p,) = perf.periods
    assert cents(p.wealth_end) == 1004.00
    assert (cents(p.price_pnl), cents(p.explicit_costs)) == (10.00, 6.00)
    assert (cents(p.net_pnl), cents(p.gross_pnl)) == (4.00, 10.00)
    m = perf.metrics
    assert m["twr_net"].value == pytest.approx(0.004)
    assert m["twr_gross"].value == pytest.approx(0.010)
    assert (cents(m["net_pnl"].value), cents(m["gross_pnl"].value)) == (4.00, 10.00)


def test_golden_5_margin_financing_accrued_then_paid_is_counted_once():
    # Day 1: buy 500.00 on margin (cash -500.00), holdings close 1,530.00, 2.00 interest
    # accrued unpaid. Day 2: the 2.00 is paid from cash; nothing else moves.
    events = [
        AccountEvent(d(1), EventKind.BUY, 500.0),
        AccountEvent(d(2), EventKind.FINANCING, 2.0),
    ]
    perf = ap.account_performance(
        [obs(0, 1000, 0), obs(1, 1530, -500, debit=2.0), obs(2, 1530, -502, debit=0.0)], events
    )
    p1, p2 = perf.periods
    assert (cents(p1.wealth_end), cents(p1.price_pnl), cents(p1.explicit_costs)) == (1028.00, 30.00, 2.00)
    assert (cents(p2.wealth_end), cents(p2.explicit_costs), cents(p2.net_pnl)) == (1028.00, 0.00, 0.00)
    assert cents(perf.metrics["explicit_costs"].value) == 2.00
    assert cents(perf.metrics["net_pnl"].value) == 28.00


def test_golden_6_deposit_and_withdrawal_separate_twr_from_mwr():
    # W: 1,000 -> 1,100 (+10%); deposit 1,000 at the start of day 2, flat; withdrawal
    # 500 at the start of day 3, holdings 1,100 -> 1,000. End wealth 1,000 + 500 cash.
    events = [
        AccountEvent(d(2), EventKind.DEPOSIT, 1000.0),
        AccountEvent(d(3), EventKind.WITHDRAWAL, 500.0),
    ]
    perf = ap.account_performance(
        [obs(0, 1000, 0), obs(1, 1100, 0), obs(2, 1100, 1000), obs(3, 1000, 500)], events
    )
    assert [cents(p.net_pnl) for p in perf.periods] == [100.00, 0.00, -100.00]
    assert [p.external for p in perf.periods] == [0.0, 1000.0, -500.0]
    m = perf.metrics
    assert m["twr_net"].value == pytest.approx(1.1 * 1.0 * (1500 / 1600) - 1)  # +3.125%
    assert m["mwr_net_annualized"].value == pytest.approx(0.0, abs=1e-6)  # money-weighted: flat
    assert m["twr_net"].value != pytest.approx(m["mwr_net_annualized"].value)
    for metric in m.values():
        assert (metric.basis, metric.start, metric.end, metric.n) == ("account", d(0), d(3), 3)
        assert metric.contract == ap.CONTRACT and metric.unavailable_reason is None


# ── the gates ────────────────────────────────────────────────────────────────


def test_an_unexplained_cash_move_is_unreconciled_and_renders_no_number():
    # Custodian cash fell 5.00 with no event to explain it.
    perf = ap.account_performance([obs(0, 1000, 500), obs(1, 1000, 495)], [])
    (p,) = perf.periods
    assert p.status == ap.UNRECONCILED and cents(p.residual) == -5.00
    assert p.net_pnl is None and p.gross_pnl is None
    for metric in perf.metrics.values():
        assert metric.value is None
        assert ap.UNRECONCILED in (metric.unavailable_reason or "")


def test_a_residual_inside_tolerance_reconciles():
    perf = ap.account_performance([obs(0, 1000, 500), obs(1, 1000, 499.995)], [])
    assert perf.periods[0].status == "reconciled"


@pytest.mark.parametrize("kw", [{"cash": None}, {"accrued": None}, {"debit": None}])
def test_an_unobserved_component_is_unavailable_never_zero(kw):
    cash = kw.pop("cash", 500)
    perf = ap.account_performance([obs(0, 1000, 500), obs(1, 1000, cash, **kw)], [])
    (p,) = perf.periods
    assert p.status == "unavailable" and "not observed" in (p.unavailable_reason or "")
    assert perf.metrics["twr_net"].value is None


def test_a_cash_source_switch_closes_the_period():
    perf = ap.account_performance(
        [obs(0, 1000, 500, source="custodian"), obs(1, 1000, 500, source="ledger")], []
    )
    assert "cash source switches" in (perf.periods[0].unavailable_reason or "")


def test_a_transfer_in_kind_is_external_at_transfer_date_value():
    events = [AccountEvent(d(1), EventKind.TRANSFER_IN, 300.0)]
    perf = ap.account_performance([obs(0, 1000, 0), obs(1, 1320, 0)], events)
    (p,) = perf.periods
    assert (p.external, cents(p.price_pnl), cents(p.net_pnl)) == (300.0, 20.00, 20.00)
    out = AccountEvent(d(1), EventKind.TRANSFER_OUT, 50.0)
    assert ap.account_external_flow(out) == -50.0


def test_corporate_action_cash_and_fx_translation_enter_the_identity():
    events = [
        AccountEvent(d(1), EventKind.CORPORATE_ACTION_CASH, 3.0),
        AccountEvent(d(1), EventKind.FX_TRANSLATION, 7.0),
    ]
    perf = ap.account_performance([obs(0, 1000, 0), obs(1, 1007, 3)], events)
    (p,) = perf.periods
    assert p.status == "reconciled"
    assert (p.corporate_action_cash, p.fx_translation, cents(p.price_pnl)) == (3.0, 7.0, 0.00)


def test_from_transaction_maps_the_canonical_ledger():
    buy = ap.from_transaction(Transaction(d(1), TxnType.BUY, "A", quantity=3, price=10.0, fees=1.0))
    assert (buy.kind, buy.amount, buy.fees) == (EventKind.BUY, 30.0, 1.0)
    reinv = ap.from_transaction(Transaction(d(1), TxnType.REINVESTMENT, "A", quantity=1, price=5.0))
    assert reinv.kind is EventKind.BUY
    split = ap.from_transaction(Transaction(d(1), TxnType.SPLIT, "A", quantity=2.0))
    assert (split.kind, split.amount) == (EventKind.SPLIT, 0.0)
    dep = ap.from_transaction(Transaction(d(1), TxnType.DEPOSIT, amount=100.0))
    assert ap.account_external_flow(dep) == 100.0
    assert {ap.from_transaction(Transaction(d(1), t, "A")).kind for t in TxnType} <= set(EventKind)


def test_events_outside_the_period_are_ignored():
    events = [AccountEvent(d(0), EventKind.DEPOSIT, 999.0), AccountEvent(d(5), EventKind.DEPOSIT, 999.0)]
    perf = ap.account_performance([obs(0, 1000, 0), obs(1, 1000, 0)], events)
    assert perf.periods[0].external == 0.0


def test_non_positive_capital_at_work_is_unavailable():
    perf = ap.account_performance([obs(0, 0, 0), obs(1, 0, 0)], [])
    assert "non-positive capital" in (perf.metrics["twr_net"].unavailable_reason or "")


def test_fewer_than_two_observations_is_an_error():
    with pytest.raises(ValueError):
        ap.account_performance([obs(0, 1000, 0)], [])
