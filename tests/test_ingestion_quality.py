"""Layer-5 ingestion data-quality gates (metron-ops#219) — the pure engine-side checks
in ``portfolio_analytics.ingestion.quality``.

Every gate runs in FLAG MODE: it returns findings and never alters its input. Each
class below covers one gate; ``TestFlagModeNeverMutates`` pins the no-mutation
property directly."""

from __future__ import annotations

import copy
import math
from datetime import date, datetime

import pytest

from portfolio_analytics.domain.ledger import RealizedGain, TxnType
from portfolio_analytics.ingestion import quality
from portfolio_analytics.ingestion.base import ConnectorSnapshot
from portfolio_analytics.ingestion.quality import (
    GATE_CONTRACT,
    GATE_PRICE_OUTLIER,
    GATE_SPLIT_DISCONTINUITY,
    Finding,
    check_price_point,
    check_price_series,
    check_snapshot_contract,
    classify_move,
)
from portfolio_analytics.ingestion.schema import (
    CanonicalAccount,
    CanonicalActivity,
    CanonicalHolding,
    CanonicalOpenLot,
    CanonicalSecurity,
)
from portfolio_analytics.prices.source import ClosePoint

TODAY = date(2024, 6, 3)
SEC = CanonicalSecurity(security_id="EQ:AAPL:USD", ticker="AAPL", currency="USD")


def _clean(**overrides) -> ConnectorSnapshot:
    snap = ConnectorSnapshot(
        source="ibkr_flex",
        accounts=[CanonicalAccount(number="U1", nav_usd=1000.0, cash_usd=100.0, tax_treatment="taxable")],
        securities=[SEC],
        holdings=[
            CanonicalHolding(account_number="U1", security_id=SEC.security_id, quantity=10, cost_basis=900,
                             avg_cost=90, market_value_local=900, as_of=datetime(2024, 6, 3, 16))
        ],
        open_lots=[CanonicalOpenLot(account_number="U1", security_id=SEC.security_id, ticker="AAPL",
                                    quantity=10, open_date=date(2024, 1, 2), cost_basis=900)],
        activities=[
            CanonicalActivity(account_number="U1", when=date(2024, 1, 2), type=TxnType.BUY,
                              security_id=SEC.security_id, quantity=10, price=90, fees=1),
            CanonicalActivity(account_number="U1", when=date(2024, 3, 1), type=TxnType.DIVIDEND,
                              security_id=SEC.security_id, amount=5),
            CanonicalActivity(account_number="U1", when=date(2024, 4, 1), type=TxnType.SPLIT,
                              security_id=SEC.security_id, quantity=2.0),
        ],
        realized_lots=[("U1", RealizedGain(ticker="AAPL", open_date=date(2023, 1, 2), close_date=date(2023, 6, 1),
                                           quantity=5, proceeds=600, cost_basis=500))],
    )
    for k, v in overrides.items():
        setattr(snap, k, v)
    return snap


def _details(findings: list[Finding]) -> str:
    return " | ".join(f.detail for f in findings)


class TestSchemaContract:
    def test_clean_snapshot_has_no_findings(self):
        assert check_snapshot_contract(_clean(), today=TODAY) == []

    def test_defaults_today_when_omitted(self):
        assert check_snapshot_contract(_clean()) == []

    @pytest.mark.parametrize(
        ("account", "needle"),
        [
            (CanonicalAccount(number=""), "account number is empty"),
            (CanonicalAccount(number="U1", nav_usd=math.nan), "nav_usd=nan is not a finite number"),
            (CanonicalAccount(number="U1", cash_usd=math.inf), "cash_usd=inf"),
            (CanonicalAccount(number="U1", currency="usd"), "not an ISO-4217 code"),
            (CanonicalAccount(number="U1", tax_treatment="roth"), "outside the 3-way vocabulary"),
            (CanonicalAccount(number="U1", as_of=datetime(2024, 6, 9)), "as_of=2024-06-09 is in the future"),
        ],
    )
    def test_account_violations(self, account, needle):
        findings = check_snapshot_contract(_clean(accounts=[account]), today=TODAY)
        assert needle in _details(findings)
        assert all(f.gate == GATE_CONTRACT for f in findings)
        assert all(f.subject.startswith("ibkr_flex:") for f in findings)

    def test_one_day_future_slack_is_tolerated(self):
        acct = CanonicalAccount(number="U1", as_of=datetime(2024, 6, 4))
        assert check_snapshot_contract(_clean(accounts=[acct]), today=TODAY) == []

    @pytest.mark.parametrize(
        ("security", "needle"),
        [
            (CanonicalSecurity(security_id="", ticker="X"), "security_id is empty"),
            (CanonicalSecurity(security_id="EQ:AAPL:USD", ticker=""), "has no ticker"),
            (CanonicalSecurity(security_id="EQ:AAPL:USD", ticker="AAPL", currency="US"), "ISO-4217"),
            (CanonicalSecurity(security_id="EQ:AAPL:USD", ticker="AAPL", asset_type="BOND"), "canonical ASSET_*"),
        ],
    )
    def test_security_violations(self, security, needle):
        findings = check_snapshot_contract(_clean(securities=[SEC, security]), today=TODAY)
        assert needle in _details(findings)

    def test_conflicting_duplicate_security_id(self):
        dup = CanonicalSecurity(security_id=SEC.security_id, ticker="AAPL", currency="HKD")
        assert "declared twice" in _details(check_snapshot_contract(_clean(securities=[SEC, dup]), today=TODAY))

    def test_identical_duplicate_security_id_is_fine(self):
        assert check_snapshot_contract(_clean(securities=[SEC, SEC]), today=TODAY) == []

    @pytest.mark.parametrize(
        ("holding", "needle"),
        [
            (CanonicalHolding(account_number="GHOST", security_id=SEC.security_id), "'GHOST' is not among"),
            (CanonicalHolding(account_number="U1", security_id="EQ:NOPE:USD"), "not in this snapshot's security"),
            (CanonicalHolding(account_number="U1", security_id=SEC.security_id, quantity=math.nan), "quantity=nan"),
            (CanonicalHolding(account_number="U1", security_id=SEC.security_id, avg_cost=-1), "avg_cost=-1"),
            (CanonicalHolding(account_number="U1", security_id=SEC.security_id, currency="$"), "ISO-4217"),
            (CanonicalHolding(account_number="U1", security_id=SEC.security_id, as_of=datetime(2025, 1, 1)),
             "in the future"),
        ],
    )
    def test_holding_violations(self, holding, needle):
        assert needle in _details(check_snapshot_contract(_clean(holdings=[holding]), today=TODAY))

    @pytest.mark.parametrize(
        ("lot", "needle"),
        [
            (CanonicalOpenLot(account_number="GHOST", security_id="s", ticker="AAPL", quantity=1,
                              open_date=date(2024, 1, 2)), "'GHOST' is not among"),
            (CanonicalOpenLot(account_number="U1", security_id="s", ticker="AAPL", quantity=0,
                              open_date=date(2024, 1, 2)), "zero quantity"),
            (CanonicalOpenLot(account_number="U1", security_id="s", ticker="AAPL", quantity=math.inf,
                              open_date=date(2024, 1, 2)), "quantity=inf"),
            (CanonicalOpenLot(account_number="U1", security_id="s", ticker="AAPL", quantity=1,
                              open_date=date(2030, 1, 2)), "open_date=2030-01-02 is in the future"),
        ],
    )
    def test_open_lot_violations(self, lot, needle):
        assert needle in _details(check_snapshot_contract(_clean(open_lots=[lot]), today=TODAY))

    @pytest.mark.parametrize(
        ("kwargs", "needle"),
        [
            ({"account_number": "GHOST"}, "'GHOST' is not among"),
            ({"security_id": "EQ:NOPE:USD"}, "not in this snapshot's security master"),
            ({"quantity": 0}, "BUY quantity=0 is not positive (the ledger skips it)"),
            ({"price": -5.0}, "BUY price=-5.0 is negative"),
            ({"fees": -1.0}, "fees=-1.0 is negative"),
            ({"security_id": ""}, "BUY has no security"),
            ({"currency": "EURO"}, "ISO-4217"),
            ({"when": date(2024, 7, 1)}, "when=2024-07-01 is in the future"),
            ({"amount": math.nan}, "amount=nan"),
        ],
    )
    def test_trade_activity_violations(self, kwargs, needle):
        base = {"account_number": "U1", "when": date(2024, 1, 2), "type": TxnType.BUY,
                "security_id": SEC.security_id, "quantity": 1, "price": 10}
        act = CanonicalActivity(**{**base, **kwargs})
        assert needle in _details(check_snapshot_contract(_clean(activities=[act]), today=TODAY))

    def test_reinvestment_is_held_to_the_purchase_contract(self):
        # REINVESTMENT goes through the ledger's BUY path (metron-ops#335), so the same
        # silently-skipped non-positive quantity must be flagged for it too.
        act = CanonicalActivity(account_number="U1", when=date(2024, 1, 2), type=TxnType.REINVESTMENT,
                                security_id=SEC.security_id, quantity=0, price=10)
        text = _details(check_snapshot_contract(_clean(activities=[act]), today=TODAY))
        assert f"{TxnType.REINVESTMENT} quantity=0 is not positive (the ledger skips it)" in text

    def test_split_violations(self):
        bad = [
            CanonicalActivity(account_number="U1", when=date(2024, 1, 2), type=TxnType.SPLIT, quantity=2.0),
            CanonicalActivity(account_number="U1", when=date(2024, 1, 2), type=TxnType.SPLIT,
                              security_id=SEC.security_id, quantity=0.0),
        ]
        text = _details(check_snapshot_contract(_clean(activities=bad), today=TODAY))
        assert "SPLIT has no security" in text
        assert "SPLIT ratio=0.0 is not positive" in text

    @pytest.mark.parametrize("ttype", [TxnType.DIVIDEND, TxnType.INTEREST, TxnType.DEPOSIT,
                                       TxnType.WITHDRAWAL, TxnType.FEE])
    def test_negative_cash_amount_violates_sign_convention(self, ttype):
        act = CanonicalActivity(account_number="U1", when=date(2024, 1, 2), type=ttype, amount=-3.0)
        assert "positive magnitudes" in _details(check_snapshot_contract(_clean(activities=[act]), today=TODAY))

    @pytest.mark.parametrize(
        ("lot", "needle"),
        [
            (("GHOST", RealizedGain("AAPL", date(2023, 1, 2), date(2023, 6, 1), 1, 10, 9)), "'GHOST' is not among"),
            (("U1", RealizedGain("AAPL", date(2023, 1, 2), date(2023, 6, 1), 0, 10, 9)), "quantity=0 is not"),
            (("U1", RealizedGain("AAPL", date(2023, 6, 2), date(2023, 6, 1), 1, 10, 9)), "before it opened"),
            (("U1", RealizedGain("AAPL", date(2023, 6, 2), date(2031, 6, 1), 1, 10, 9)), "in the future"),
            (("U1", RealizedGain("AAPL", date(2023, 1, 2), date(2023, 6, 1), 1, math.nan, 9)), "proceeds=nan"),
        ],
    )
    def test_realized_lot_violations(self, lot, needle):
        assert needle in _details(check_snapshot_contract(_clean(realized_lots=[lot]), today=TODAY))

    def test_non_numeric_value_is_not_finite(self):
        # A bool (or a string a sloppy parser left behind) is not a number the ledger can use.
        acct = CanonicalAccount(number="U1", nav_usd=True)  # type: ignore[arg-type]
        assert "nav_usd=True is not a finite number" in _details(check_snapshot_contract(_clean(accounts=[acct])))

    def test_unnamed_source_is_labelled(self):
        findings = check_snapshot_contract(_clean(source="", accounts=[CanonicalAccount(number="")]), today=TODAY)
        assert findings[0].subject.startswith("?:")


class TestPriceOutliers:
    D1, D2 = date(2024, 3, 28), date(2024, 4, 1)

    def _move(self, before, after, splits=()):
        return classify_move("AAPL", ClosePoint(self.D1, before), ClosePoint(self.D2, after), splits)

    def test_ordinary_move_is_not_flagged(self):
        assert self._move(100, 105) is None
        assert self._move(100, 80) is None

    def test_just_inside_threshold_is_not_flagged(self):
        assert self._move(100, 129.99) is None  # just inside PRICE_OUTLIER_MAX_MOVE (30%)
        assert self._move(100, 70.01) is None

    def test_unexplained_drop_is_an_outlier(self):
        f = self._move(100, 50)
        assert f.gate == GATE_PRICE_OUTLIER
        assert f.observed == pytest.approx(-0.5)
        assert f.threshold == quality.PRICE_OUTLIER_MAX_MOVE
        assert f.as_of == self.D2
        assert "no recorded split" in f.detail

    def test_unexplained_spike_is_an_outlier(self):
        assert self._move(100, 150).gate == GATE_PRICE_OUTLIER

    def test_recorded_split_turns_the_jump_into_a_continuity_finding(self):
        """The issue's named corrupter: a 2-for-1 on an unadjusted series reads as −50%."""
        f = self._move(100, 49, splits=[(date(2024, 4, 1), 2.0)])
        assert f.gate == GATE_SPLIT_DISCONTINUITY
        assert "2:1 split" in f.detail and "not adjusted" in f.detail

    def test_reverse_split(self):
        assert self._move(1.0, 10.2, splits=[(date(2024, 3, 29), 0.1)]).gate == GATE_SPLIT_DISCONTINUITY

    def test_split_date_slack_either_side(self):
        # A split stamped a couple of days off the bar dates (ex vs pay date) still explains the move.
        assert self._move(100, 50, splits=[(date(2024, 3, 26), 2.0)]).gate == GATE_SPLIT_DISCONTINUITY
        assert self._move(100, 50, splits=[(date(2024, 4, 4), 2.0)]).gate == GATE_SPLIT_DISCONTINUITY

    def test_split_outside_the_window_explains_nothing(self):
        assert self._move(100, 50, splits=[(date(2024, 1, 2), 2.0)]).gate == GATE_PRICE_OUTLIER

    def test_ratio_that_does_not_match_explains_nothing(self):
        # A −35% day is not a 2-for-1 (0.65 × 2 = 1.30, well outside the 10% tolerance).
        assert self._move(100, 65, splits=[(date(2024, 4, 1), 2.0)]).gate == GATE_PRICE_OUTLIER

    def test_invalid_split_ratios_are_ignored(self):
        assert self._move(100, 50, splits=[(date(2024, 4, 1), 0.0), (date(2024, 4, 1), math.nan)]).gate \
            == GATE_PRICE_OUTLIER

    def test_two_splits_in_window_compound(self):
        assert self._move(100, 25, splits=[(date(2024, 4, 1), 2.0), (date(2024, 3, 29), 2.0)]).gate \
            == GATE_SPLIT_DISCONTINUITY

    def test_unusable_closes_are_left_to_the_contract_gate(self):
        assert self._move(0, 50) is None
        assert self._move(100, math.nan) is None
        assert self._move(100, -1) is None


class TestPriceSeries:
    def test_series_is_sorted_and_every_seam_judged(self):
        pts = [ClosePoint(date(2024, 1, 4), 50), ClosePoint(date(2024, 1, 2), 100), ClosePoint(date(2024, 1, 3), 100)]
        findings = check_price_series("X", pts, today=TODAY)
        assert [f.gate for f in findings] == [GATE_PRICE_OUTLIER]
        assert findings[0].as_of == date(2024, 1, 4)

    def test_prior_cached_close_is_the_first_seam(self):
        findings = check_price_series("X", [ClosePoint(date(2024, 1, 3), 50)], prior=ClosePoint(date(2024, 1, 2), 100))
        assert [f.gate for f in findings] == [GATE_PRICE_OUTLIER]

    def test_prior_not_before_series_is_ignored(self):
        assert check_price_series("X", [ClosePoint(date(2024, 1, 3), 50)], prior=ClosePoint(date(2024, 1, 3), 100)) \
            == []

    def test_duplicate_dates_keep_the_last_value(self):
        pts = [ClosePoint(date(2024, 1, 2), 100), ClosePoint(date(2024, 1, 3), 10), ClosePoint(date(2024, 1, 3), 101)]
        assert check_price_series("X", pts) == []

    def test_empty_series(self):
        assert check_price_series("X", [], prior=ClosePoint(date(2024, 1, 2), 100)) == []

    @pytest.mark.parametrize("close", [0.0, -3.0, math.nan, math.inf])
    def test_price_contract(self, close):
        findings = check_price_point("X", ClosePoint(date(2024, 1, 2), close), today=TODAY)
        assert [f.gate for f in findings] == [GATE_CONTRACT]

    def test_future_bar_violates_contract(self):
        findings = check_price_point("X", ClosePoint(date(2024, 6, 10), 10.0), today=TODAY)
        assert "in the future" in findings[0].detail

    def test_default_today(self):
        assert check_price_point("X", ClosePoint(date(2024, 1, 2), 10.0)) == []


class TestFlagModeNeverMutates:
    def test_contract_check_leaves_the_snapshot_untouched(self):
        dirty = _clean(activities=[CanonicalActivity(account_number="GHOST", when=date(2030, 1, 1),
                                                     type=TxnType.BUY, quantity=0)])
        before = copy.deepcopy(dirty)
        assert check_snapshot_contract(dirty, today=TODAY)  # it found things…
        assert dirty == before  # …and changed nothing, dropped nothing

    def test_series_check_leaves_the_input_untouched(self):
        pts = [ClosePoint(date(2024, 1, 3), 50), ClosePoint(date(2024, 1, 2), 100)]
        splits = [(date(2024, 1, 3), 2.0)]
        before_pts, before_splits = list(pts), list(splits)
        assert check_price_series("X", pts, splits)
        assert pts == before_pts and splits == before_splits


def test_finding_renders_grep_stable_prefix():
    f = Finding(GATE_PRICE_OUTLIER, "AAPL", "close moved")
    assert f.render() == "[data-quality:price_outlier] AAPL: close moved"
    assert set(quality.GATES) == {"schema_contract", "price_outlier", "split_discontinuity", "stale_price"}
