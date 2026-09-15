"""New cash to my targets (metron-ops-I311) — the optimizer unit tests: cap binding,
whole-share rounding, unallocated-with-reason, and the never-widens-candidates invariant.

Pure unit tests against an in-memory DB with an injected price reader — no S3, no
network, no ranking of any kind (mirrors tests/test_deploy_cash.py's fixture shape, minus
the rating/champion machinery this feature deliberately has none of).
"""

from __future__ import annotations

from datetime import date

import pytest

from api.db import models
from api.services import cash_to_targets
from portfolio_analytics.prices import ClosePoint

_AS_OF = date(2026, 9, 15)


def _seed(session, *, held: dict[str, tuple[float, float]] | None = None):
    """``held`` is ``{ticker: (shares, price)}``."""
    held = held or {}
    tenant = models.Tenant(name="t")
    session.add(tenant)
    session.flush()
    pf = models.Portfolio(tenant_id=tenant.id, name="P", base_currency="USD")
    session.add(pf)
    session.flush()
    acct = models.Account(tenant_id=tenant.id, portfolio_id=pf.id, broker="csv", external_id="CSV-1", currency="USD")
    session.add(acct)
    session.flush()
    for sym, (shares, price) in held.items():
        sec = models.Security(symbol=sym, currency="USD")
        session.add(sec)
        session.flush()
        session.add(models.Transaction(
            tenant_id=tenant.id, account_id=acct.id, security_id=sec.id, txn_type="BUY",
            quantity=shares, price=price, amount=shares * price, currency="USD",
            trade_date=date(2026, 1, 2), source_key=f"buy-{sym}",
        ))
        session.add(models.PriceBar(security_id=sec.id, bar_date=date(2026, 9, 13), close=price, currency="USD"))
    session.commit()
    return tenant.id, pf.id


def _price_reader(prices: dict[str, float]):
    def _read(session, symbols, *, currency_by_symbol=None):
        return {s: ClosePoint(bar_date=_AS_OF, close=prices[s]) for s in symbols if s in prices}
    return _read


def _config(targets: list[tuple[str, float]], *, cap=None, min_line=0.0):
    return cash_to_targets.PlanTargetsConfig(
        targets=tuple(cash_to_targets.TargetLine(symbol=s, weight=w) for s, w in targets),
        max_single_position=cap, min_line_usd=min_line,
    )


def _by_symbol(plan):
    return {line.symbol: line for line in plan.lines}


class TestNeverWidensCandidates:
    def test_only_targeted_symbols_can_receive_a_line(self, db_session):
        # Held AAPL is NOT in the target list — it must never receive a purchase.
        tid, pid = _seed(db_session, held={"AAPL": (10, 100.0)})
        config = _config([("MSFT", 0.5)])
        plan = cash_to_targets.build_plan(
            db_session, tid, pid, 1_000.0, config, as_of=_AS_OF,
            price_reader=_price_reader({"MSFT": 200.0}),
        )
        assert set(_by_symbol(plan)) <= {"MSFT"}
        assert "AAPL" not in _by_symbol(plan)

    def test_empty_targets_allocates_nothing(self, db_session):
        tid, pid = _seed(db_session)
        config = _config([])
        plan = cash_to_targets.build_plan(db_session, tid, pid, 500.0, config, as_of=_AS_OF)
        assert plan.lines == []
        assert plan.allocated_usd == 0.0
        assert plan.unallocated_usd == 500.0


class TestOrderingIsTheUsersOwnList:
    def test_rows_ordered_by_target_list_not_by_deviation_size(self, db_session):
        tid, pid = _seed(db_session)
        # KO listed first with a SMALLER gap than MSFT — output order must still be KO, MSFT.
        config = _config([("KO", 0.05), ("MSFT", 0.50)])
        plan = cash_to_targets.build_plan(
            db_session, tid, pid, 10_000.0, config, as_of=_AS_OF,
            price_reader=_price_reader({"KO": 60.0, "MSFT": 300.0}),
        )
        assert [line.symbol for line in plan.lines] == ["KO", "MSFT"]


class TestCapBinding:
    def test_position_cap_limits_the_line_even_with_plenty_of_cash(self, db_session):
        tid, pid = _seed(db_session)
        # basis = 0 (no holdings) + 10,000 deployed = 10,000. cap 10% => $1,000 ceiling.
        config = _config([("AAPL", 0.50)], cap=0.10)
        plan = cash_to_targets.build_plan(
            db_session, tid, pid, 10_000.0, config, as_of=_AS_OF,
            price_reader=_price_reader({"AAPL": 100.0}),
        )
        line = _by_symbol(plan)["AAPL"]
        assert line.usd <= 1_000.0 + 1e-6
        assert line.weight_after == pytest.approx(0.10, abs=1e-4)
        assert plan.unallocated_usd > 0

    def test_already_at_cap_is_skipped_with_reason(self, db_session):
        tid, pid = _seed(db_session, held={"AAPL": (100, 10.0)})  # $1,000 held
        config = _config([("AAPL", 0.50)], cap=0.10)  # basis = 1000 + 1000 = 2000; cap = 200 < held 1000
        plan = cash_to_targets.build_plan(
            db_session, tid, pid, 1_000.0, config, as_of=_AS_OF,
            price_reader=_price_reader({"AAPL": 10.0}),
        )
        assert _by_symbol(plan) == {}
        reasons = {row["symbol"]: row["reason"] for row in plan.skipped}
        assert reasons["AAPL"] == "at_or_above_cap"
        assert "the position-cap limit was reached for some targets" in plan.unallocated_reasons


class TestRounding:
    def test_whole_shares_only(self, db_session):
        tid, pid = _seed(db_session)
        config = _config([("AAPL", 1.0)])
        # $999 at $301/share → floor(999/301) = 3 shares = $903, not a fractional share.
        plan = cash_to_targets.build_plan(
            db_session, tid, pid, 999.0, config, as_of=_AS_OF,
            price_reader=_price_reader({"AAPL": 301.0}),
        )
        line = _by_symbol(plan)["AAPL"]
        assert line.shares == 3
        assert line.usd == pytest.approx(903.0)
        assert plan.unallocated_usd == pytest.approx(96.0)


class TestUnallocatedReason:
    def test_min_line_usd_drops_a_too_small_remainder(self, db_session):
        tid, pid = _seed(db_session)
        config = _config([("AAPL", 1.0)], min_line=50.0)
        # $40 total is below the $50 minimum line — no line at all, all unallocated.
        plan = cash_to_targets.build_plan(
            db_session, tid, pid, 40.0, config, as_of=_AS_OF,
            price_reader=_price_reader({"AAPL": 10.0}),
        )
        assert plan.lines == []
        assert plan.unallocated_usd == pytest.approx(40.0)
        assert plan.unallocated_reasons  # non-empty, human-readable

    def test_unpriced_target_is_skipped_and_reported(self, db_session):
        tid, pid = _seed(db_session)
        config = _config([("ZZZZ", 1.0)])
        plan = cash_to_targets.build_plan(db_session, tid, pid, 500.0, config, as_of=_AS_OF, price_reader=_price_reader({}))
        assert plan.lines == []
        reasons = {row["symbol"]: row["reason"] for row in plan.skipped}
        assert reasons["ZZZZ"] == "unpriced"


class TestDisclaimerAndEcho:
    def test_config_echoed_and_disclaimer_verbatim(self, db_session):
        tid, pid = _seed(db_session)
        config = _config([("AAPL", 1.0)], cap=0.25, min_line=10.0)
        plan = cash_to_targets.build_plan(
            db_session, tid, pid, 100.0, config, as_of=_AS_OF, price_reader=_price_reader({"AAPL": 10.0}),
        )
        assert plan.config.max_single_position == 0.25
        assert plan.config.min_line_usd == 10.0
        assert plan.disclaimer == "Arithmetic against the targets you set. Metron does not choose securities."
