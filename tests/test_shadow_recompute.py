"""Tests for the layer-3 dashboard-accuracy shadow recompute (metron-ops#218).

Three groups:
  1. Correctness against the SAME hand-verified golden fixture
     ``tests/test_golden_portfolios.py`` uses (independently re-checked here through
     ``shadow_nav_series``/``shadow_twr`` — not re-deriving the golden numbers, cross-
     checking against them).
  2. The diff logic (tolerance bands, unpriced-date skipping, break persistence /
     resolve / re-open lifecycle) with synthetic fixtures.
  3. A structural-independence proof: a per-account-misattribution bug that changes
     ``reconstruct_snapshots``' (production's) per-account-FIFO-relief NAV is INVISIBLE
     to a same-code-path second call, but IS caught by diffing against this shadow's
     portfolio-wide-FIFO relief — demonstrating the two aggregation orders actually
     diverge under a real bug class, not just in the abstract.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest

from api.db import models
from api.services import shadow_recompute
from api.services.shadow_recompute import (
    Divergence,
    ShadowPoint,
    diff_nav_series,
    diff_realized_pnl,
    diff_twr,
    shadow_nav_series,
    shadow_twr,
)
from portfolio_analytics.domain.ledger import Transaction, TxnType, build_ledger

# ── Seeding helpers (mirrors tests/test_account_cash.py's convention) ───────────────


def _seed_portfolio(session, name="P"):
    tenant = models.Tenant(name=f"t-{name}")
    session.add(tenant)
    session.flush()
    pf = models.Portfolio(tenant_id=tenant.id, name=name, base_currency="USD")
    session.add(pf)
    session.flush()
    return tenant, pf


def _add_account(session, tenant, pf, external_id="A1"):
    acct = models.Account(
        tenant_id=tenant.id, portfolio_id=pf.id, broker="csv", external_id=external_id, currency="USD",
    )
    session.add(acct)
    session.flush()
    return acct


def _add_security(session, symbol, currency="USD"):
    sec = models.Security(symbol=symbol, currency=currency)
    session.add(sec)
    session.flush()
    return sec


def _add_txn(session, tenant, acct, sec, *, txn_type, qty=0.0, price=0.0, amount=0.0, fees=0.0, when, key):
    session.add(
        models.Transaction(
            tenant_id=tenant.id, account_id=acct.id, security_id=sec.id if sec else None,
            txn_type=txn_type, quantity=qty, price=price, amount=amount, fees=fees,
            currency="USD", trade_date=when, source_key=key,
        )
    )


def _add_price(session, sec, when, close):
    session.add(models.PriceBar(security_id=sec.id, bar_date=when, close=close, currency="USD"))


# ── 1. Correctness against the hand-verified golden fixture ─────────────────────────


class TestShadowMatchesGoldenFixture:
    """Same transactions as test_golden_portfolios.py's from-scratch golden fixture
    (10 sh @150 fee5, 10 sh @170 fee5, sell 12 @200 fee12, dividend 16) — hand-verified
    realized gain $542.00, remaining 8 sh, unrealized $316.00 @ $210 mark."""

    def test_shadow_realized_matches_hand_computed(self, db_session):
        tenant, pf = _seed_portfolio(db_session, "golden")
        acct = _add_account(db_session, tenant, pf)
        sec = _add_security(db_session, "AAPL")
        _add_txn(db_session, tenant, acct, sec, txn_type="BUY", qty=10, price=150.0, fees=5.0,
                 when=date(2025, 1, 2), key="k1")
        _add_txn(db_session, tenant, acct, sec, txn_type="BUY", qty=10, price=170.0, fees=5.0,
                 when=date(2025, 2, 15), key="k2")
        _add_txn(db_session, tenant, acct, sec, txn_type="SELL", qty=12, price=200.0, fees=12.0,
                 when=date(2025, 6, 1), key="k3")
        _add_txn(db_session, tenant, acct, sec, txn_type="DIVIDEND", amount=16.0, when=date(2025, 7, 1), key="k4")
        # Only the LAST date has a cached close — earlier dates are correctly reported
        # unpriced (never fabricated) rather than back-filled from a later price.
        _add_price(db_session, sec, date(2025, 7, 1), 210.0)
        db_session.commit()

        points, unpriced = shadow_nav_series(db_session, tenant.id, pf.id, through=date(2025, 7, 1))
        assert unpriced == ["AAPL"]  # earlier dates have no cached close yet
        last = points[-1]
        assert last.when == date(2025, 7, 1)
        assert last.cumulative_realized == pytest.approx(542.0)
        # NAV at the last point = 8 remaining shares @ $210 mark = $1,680.00.
        assert last.nav == pytest.approx(1680.0)


# ── 2. Diff logic: tolerance bands, unpriced-date skip, break lifecycle ─────────────


class TestDiffNavSeries:
    def test_within_tolerance_no_divergence(self):
        pid = uuid.uuid4()
        prod = [(date(2025, 1, 1), 10_000.0)]
        shadow = [ShadowPoint(when=date(2025, 1, 1), nav=10_000.50, cumulative_realized=0.0, flow=0.0)]
        assert diff_nav_series(pid, prod, shadow) == []

    def test_beyond_tolerance_flags_divergence(self):
        pid = uuid.uuid4()
        prod = [(date(2025, 1, 1), 10_000.0)]
        shadow = [ShadowPoint(when=date(2025, 1, 1), nav=10_500.0, cumulative_realized=0.0, flow=0.0)]
        out = diff_nav_series(pid, prod, shadow)
        assert len(out) == 1
        assert out[0].metric == "nav"
        assert out[0].production_value == 10_000.0
        assert out[0].shadow_value == 10_500.0

    def test_unpriced_date_is_skipped_not_flagged(self):
        """A date whose shadow NAV is known-incomplete (unpriced leg) must never be
        diffed — comparing a partial shadow number against a complete production
        number would manufacture a false divergence, not catch a real one."""
        pid = uuid.uuid4()
        prod = [(date(2025, 1, 1), 10_000.0)]
        shadow = [ShadowPoint(when=date(2025, 1, 1), nav=1.0, cumulative_realized=0.0, flow=0.0)]
        out = diff_nav_series(pid, prod, shadow, unpriced_dates={date(2025, 1, 1)})
        assert out == []

    def test_missing_shadow_date_is_skipped(self):
        pid = uuid.uuid4()
        prod = [(date(2025, 1, 1), 10_000.0), (date(2025, 1, 2), 10_100.0)]
        shadow = [ShadowPoint(when=date(2025, 1, 1), nav=10_000.0, cumulative_realized=0.0, flow=0.0)]
        out = diff_nav_series(pid, prod, shadow)
        assert out == []  # 1/2 has no shadow counterpart yet — not a divergence


class TestDiffRealizedPnl:
    """diff_realized_pnl is not yet wired into shadow_recompute_portfolio (see its
    docstring — no persisted production "realized to date" figure exists yet to diff
    against), but is exported + tested so wiring it is a one-line follow-up."""

    def test_within_tolerance(self):
        assert diff_realized_pnl(uuid.uuid4(), date(2025, 1, 1), 500.0, 500.50) is None

    def test_beyond_tolerance(self):
        d = diff_realized_pnl(uuid.uuid4(), date(2025, 1, 1), 500.0, 600.0)
        assert d is not None
        assert d.metric == "realized_pnl"
        assert d.production_value == 500.0
        assert d.shadow_value == 600.0

    def test_none_on_either_side_never_diffed(self):
        assert diff_realized_pnl(uuid.uuid4(), date(2025, 1, 1), None, 500.0) is None
        assert diff_realized_pnl(uuid.uuid4(), date(2025, 1, 1), 500.0, None) is None


class TestDiffTwr:
    def test_within_tolerance(self):
        assert diff_twr(uuid.uuid4(), date(2025, 1, 1), 0.10, 0.1005) is None

    def test_beyond_tolerance(self):
        d = diff_twr(uuid.uuid4(), date(2025, 1, 1), 0.10, 0.15)
        assert d is not None
        assert d.metric == "twr"

    def test_none_on_either_side_never_diffed(self):
        assert diff_twr(uuid.uuid4(), date(2025, 1, 1), None, 0.10) is None
        assert diff_twr(uuid.uuid4(), date(2025, 1, 1), 0.10, None) is None


class TestShadowTwr:
    def test_two_points_no_flow(self):
        points = [
            ShadowPoint(when=date(2025, 1, 1), nav=1000.0, cumulative_realized=0.0, flow=0.0),
            ShadowPoint(when=date(2025, 2, 1), nav=1100.0, cumulative_realized=0.0, flow=0.0),
        ]
        assert shadow_twr(points) == pytest.approx(0.10)

    def test_flow_neutralized(self):
        # NAV doubles from 1000 to 2200, but $1000 of that is a fresh contribution
        # (flow) on the second date — true investment return is (2200-1000)/1000 - 1 = 0.20.
        points = [
            ShadowPoint(when=date(2025, 1, 1), nav=1000.0, cumulative_realized=0.0, flow=0.0),
            ShadowPoint(when=date(2025, 2, 1), nav=2200.0, cumulative_realized=0.0, flow=1000.0),
        ]
        assert shadow_twr(points) == pytest.approx(0.20)

    def test_single_point_is_none(self):
        assert shadow_twr([ShadowPoint(when=date(2025, 1, 1), nav=1000.0, cumulative_realized=0.0, flow=0.0)]) is None


# ── Break persistence lifecycle (mirrors tests/test_reconciliation.py's shape) ──────


class TestBreakPersistenceLifecycle:
    def test_new_divergence_creates_row_and_counts_as_new(self, db_session):
        tenant, pf = _seed_portfolio(db_session, "breaks")
        db_session.commit()
        divs = [Divergence(pf.id, date(2025, 1, 1), "nav", 10_000.0, 10_500.0, 5.0)]
        rows, new_count = shadow_recompute._persist_breaks(db_session, tenant.id, divs, date(2025, 1, 1))
        db_session.commit()
        assert new_count == 1
        assert len(rows) == 1
        stored = db_session.query(models.ShadowRecomputeBreak).one()
        assert stored.break_type == "nav"
        assert stored.production_value == pytest.approx(10_000.0)
        assert stored.shadow_value == pytest.approx(10_500.0)
        assert stored.resolved_at is None

    def test_reproducing_break_upserts_not_duplicates(self, db_session):
        tenant, pf = _seed_portfolio(db_session, "breaks2")
        db_session.commit()
        divs = [Divergence(pf.id, date(2025, 1, 1), "nav", 10_000.0, 10_500.0, 5.0)]
        shadow_recompute._persist_breaks(db_session, tenant.id, divs, date(2025, 1, 1))
        db_session.commit()
        # Same break_key (portfolio/metric/as_of) reproduces the next run with updated values.
        divs2 = [Divergence(pf.id, date(2025, 1, 1), "nav", 10_000.0, 10_600.0, 5.0)]
        rows, new_count = shadow_recompute._persist_breaks(db_session, tenant.id, divs2, date(2025, 1, 2))
        db_session.commit()
        assert new_count == 0  # not newly opened — it's the same still-open break
        assert db_session.query(models.ShadowRecomputeBreak).count() == 1
        assert rows[0].shadow_value == pytest.approx(10_600.0)

    def test_resolved_break_reopens_on_recurrence(self, db_session):
        tenant, pf = _seed_portfolio(db_session, "breaks3")
        db_session.commit()
        key = shadow_recompute._break_key(pf.id, "nav", date(2025, 1, 1))
        row = models.ShadowRecomputeBreak(
            tenant_id=tenant.id, portfolio_id=pf.id, break_type="nav", break_key=key,
            break_date=date(2025, 1, 1), as_of_date=date(2025, 1, 1),
            production_value=10_000.0, shadow_value=10_500.0, tolerance=5.0,
        )
        row.resolved_at = row.created_at = None
        from datetime import UTC, datetime
        row.resolved_at = datetime.now(UTC)
        db_session.add(row)
        db_session.commit()

        divs = [Divergence(pf.id, date(2025, 1, 1), "nav", 10_000.0, 10_500.0, 5.0)]
        rows, new_count = shadow_recompute._persist_breaks(db_session, tenant.id, divs, date(2025, 1, 5))
        db_session.commit()
        assert new_count == 1  # reopening counts as a fresh occurrence for alerting
        assert rows[0].resolved_at is None

    def test_stale_break_resolves_when_not_reproduced(self, db_session):
        tenant, pf = _seed_portfolio(db_session, "breaks4")
        db_session.commit()
        key = shadow_recompute._break_key(pf.id, "nav", date(2025, 1, 1))
        row = models.ShadowRecomputeBreak(
            tenant_id=tenant.id, portfolio_id=pf.id, break_type="nav", break_key=key,
            break_date=date(2025, 1, 1), as_of_date=date(2025, 1, 1),
            production_value=10_000.0, shadow_value=10_500.0, tolerance=5.0,
        )
        db_session.add(row)
        db_session.commit()

        resolved = shadow_recompute._resolve_stale_breaks(db_session, tenant.id, pf.id, still_open_keys=set())
        db_session.commit()
        assert resolved == 1
        db_session.refresh(row)
        assert row.resolved_at is not None


# ── End-to-end: shadow_recompute_portfolio / shadow_recompute_all ───────────────────


class TestShadowRecomputeEndToEnd:
    def test_divergent_nav_snapshot_creates_break_and_alerts(self, db_session, monkeypatch):
        alerts: list[str] = []
        monkeypatch.setattr(
            shadow_recompute, "send_telegram_alert", lambda text: (alerts.append(text), True)[1]
        )
        tenant, pf = _seed_portfolio(db_session, "e2e")
        acct = _add_account(db_session, tenant, pf)
        sec = _add_security(db_session, "AAPL")
        _add_txn(db_session, tenant, acct, sec, txn_type="BUY", qty=10, price=100.0,
                 when=date(2025, 1, 1), key="k1")
        _add_price(db_session, sec, date(2025, 1, 1), 100.0)
        # Production served a NAV wildly different from the true 10*100=1000 shadow
        # value (simulating e.g. metron-ops#74's sync-race inflation bug).
        db_session.add(models.NavSnapshot(
            tenant_id=tenant.id, portfolio_id=pf.id, snap_date=date(2025, 1, 1), nav=12_000.0,
        ))
        db_session.commit()

        result = shadow_recompute.shadow_recompute_portfolio(db_session, pf, today=date(2025, 1, 1))

        assert result.breaks_new == 1
        assert result.breaks_open == 1
        assert len(alerts) == 1
        assert "shadow-recompute" in alerts[0]
        stored = db_session.query(models.ShadowRecomputeBreak).one()
        assert stored.break_type == "nav"
        assert stored.production_value == pytest.approx(12_000.0)
        assert stored.shadow_value == pytest.approx(1000.0)
        assert stored.alerted_at is not None

    def test_agreeing_nav_snapshot_creates_no_break(self, db_session, monkeypatch):
        monkeypatch.setattr(shadow_recompute, "send_telegram_alert", lambda text: True)
        tenant, pf = _seed_portfolio(db_session, "e2e-clean")
        acct = _add_account(db_session, tenant, pf)
        sec = _add_security(db_session, "AAPL")
        _add_txn(db_session, tenant, acct, sec, txn_type="BUY", qty=10, price=100.0,
                 when=date(2025, 1, 1), key="k1")
        _add_price(db_session, sec, date(2025, 1, 1), 100.0)
        db_session.add(models.NavSnapshot(
            tenant_id=tenant.id, portfolio_id=pf.id, snap_date=date(2025, 1, 1), nav=1000.0,
        ))
        db_session.commit()

        result = shadow_recompute.shadow_recompute_portfolio(db_session, pf, today=date(2025, 1, 1))

        assert result.breaks_new == 0
        assert result.breaks_open == 0
        assert db_session.query(models.ShadowRecomputeBreak).count() == 0

    def test_shadow_recompute_all_excludes_reference_portfolio(self, db_session, monkeypatch):
        """Mirrors reconciliation.reconcile_all's / daily_refresh's exclusion (metron-ops#141):
        the Showcase Portfolio's NAV series is sole-sourced from the engine's published
        artifact, not from Metron's own transaction ledger — nothing for either the
        production or the shadow path to independently recompute."""
        monkeypatch.setattr(shadow_recompute, "send_telegram_alert", lambda text: True)
        from api.services.demo import REFERENCE_PORTFOLIO_ID

        tenant = models.Tenant(name="demo-tenant")
        db_session.add(tenant)
        db_session.flush()
        ref_pf = models.Portfolio(id=REFERENCE_PORTFOLIO_ID, tenant_id=tenant.id, name="Showcase", base_currency="USD")
        db_session.add(ref_pf)
        db_session.commit()

        total = shadow_recompute.shadow_recompute_all(db_session)
        assert total.portfolios_checked == 0

    def test_shadow_recompute_all_covers_normal_portfolios(self, db_session, monkeypatch):
        monkeypatch.setattr(shadow_recompute, "send_telegram_alert", lambda text: True)
        tenant, pf = _seed_portfolio(db_session, "e2e-all")
        db_session.commit()

        total = shadow_recompute.shadow_recompute_all(db_session)
        assert total.portfolios_checked >= 1

    def test_per_portfolio_exception_is_isolated_and_recorded(self, db_session, monkeypatch):
        """A failure recomputing one portfolio must alert + record in .errors, never
        raise and abort the whole nightly batch (mirrors reconciliation's fetch-failure
        posture)."""
        alerts: list[str] = []
        monkeypatch.setattr(
            shadow_recompute, "send_telegram_alert", lambda text: (alerts.append(text), True)[1]
        )

        def _boom(*a, **k):
            raise RuntimeError("boom")

        monkeypatch.setattr(shadow_recompute, "shadow_nav_series", _boom)
        tenant, pf = _seed_portfolio(db_session, "e2e-error")
        db_session.commit()

        result = shadow_recompute.shadow_recompute_portfolio(db_session, pf, today=date(2025, 1, 1))
        assert len(result.errors) == 1
        assert "boom" in result.errors[0]
        assert any("boom" in a for a in alerts)


# ── 3. Structural independence: a per-account-misattribution bug ────────────────────


class TestStructuralIndependenceFromProductionAggregation:
    """Reproduces the exact bug class the issue calls out: a bug that lives in
    production's PER-ACCOUNT FIFO relief (``analytics.build_portfolio_ledger``, which
    ``performance.reconstruct_snapshots`` builds on via ``_load_lot_timeline``) — a
    transaction misattributed to the wrong account. A second call to the SAME
    per-account-scoped code path reproduces the exact same (wrong) per-account lot
    split and therefore would NOT catch it. This shadow module's ledger is built with
    NO account partitioning at all (one merged ``build_ledger`` call over the pooled
    transaction stream, see ``shadow_nav_series``), so the account-attribution bug
    cannot distort ITS FIFO relief in the first place — the shadow's realized/NAV stays
    correct regardless of which account a transaction is (mis)filed under, which is
    exactly why comparing the two catches an account-attribution class of bug that a
    same-path rerun structurally cannot."""

    def _two_account_transactions(self):
        """Two accounts both trade AAPL. Account A buys low, account B buys high; a
        SELL is recorded (correctly) against account A. Correct per-account FIFO must
        close A's own $100 lot, not B's $200 lot."""
        return {
            "A": [
                Transaction(date(2025, 1, 1), TxnType.BUY, ticker="AAPL", quantity=10, price=100.0),
            ],
            "B": [
                Transaction(date(2025, 1, 5), TxnType.BUY, ticker="AAPL", quantity=10, price=200.0),
            ],
            "sell_on_A": Transaction(date(2025, 6, 1), TxnType.SELL, ticker="AAPL", quantity=10, price=150.0),
        }

    def test_per_account_misattribution_changes_production_style_realized_but_not_shadow(self):
        fx = self._two_account_transactions()

        # Correct world: the SELL is attributed to account A (closes A's $100 lot).
        # Production-style per-account ledger (mirrors analytics.build_portfolio_ledger's
        # per-account FIFO scoping — one build_ledger call per account).
        correct_ledger_a = build_ledger([*fx["A"], fx["sell_on_A"]])
        correct_realized_a = sum(r.gain for r in correct_ledger_a.realized)
        assert correct_realized_a == pytest.approx(500.0)  # (150-100)*10, closes the cheap lot

        # BUGGY world: an account-attribution bug misfiles the SELL onto account B
        # instead of A. Production's per-account-scoped aggregation now closes B's
        # $200 lot instead of A's $100 lot — a materially different (and wrong) realized
        # gain — and a second call to the SAME per-account code path reproduces this
        # identical (wrong) number every time; it can never disagree with itself.
        buggy_ledger_b = build_ledger([*fx["B"], fx["sell_on_A"]])
        buggy_realized_b = sum(r.gain for r in buggy_ledger_b.realized)
        assert buggy_realized_b == pytest.approx(-500.0)  # (150-200)*10 — wrong lot closed
        assert buggy_realized_b != pytest.approx(correct_realized_a)

        # Shadow world: ONE merged ledger over the pooled transaction stream, no
        # account partitioning at all — the misattribution never has anywhere to bite,
        # because the shadow doesn't look at which account a transaction is filed
        # under. It always closes the oldest lot chronologically (A's $100 lot,
        # regardless of which account the SELL was mis-recorded against).
        shadow_ledger = build_ledger([*fx["A"], *fx["B"], fx["sell_on_A"]])
        shadow_realized = sum(r.gain for r in shadow_ledger.realized)
        assert shadow_realized == pytest.approx(500.0)  # matches the CORRECT answer

        # The load-bearing assertion: shadow (correct) disagrees with the buggy
        # production-style per-account result by exactly the amount the misattribution
        # bug introduced — this is the divergence the nightly diff would alert on.
        assert shadow_realized != pytest.approx(buggy_realized_b)
        assert abs(shadow_realized - buggy_realized_b) == pytest.approx(1000.0)
