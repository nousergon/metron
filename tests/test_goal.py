"""Retirement-goal facets (metron-ops-I316) — service arithmetic, router, entitlement
and registry contract coverage.

Doctrine: every value here traces to a number a test explicitly types in as the
"user-authored" goal — nothing here fabricates a target, date, contribution, or rate.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest

from api import entitlements
from api.db import models
from api.insights.registry import CATALOG, FACET_BY_KEY
from api.services import goal as goal_service
from api.services import performance

# ── golden arithmetic — _years_to_target ─────────────────────────────────────


class TestYearsToTarget:
    def test_already_at_target_is_zero_years(self):
        assert goal_service._years_to_target(100_000, 100_000, 0, 0.05) == 0.0

    def test_positive_rate_zero_contribution(self):
        # 50k -> 100k at 7%/yr, no contributions: ln(2)/ln(1.07) ≈ 10.24y.
        years = goal_service._years_to_target(50_000, 100_000, 0, 0.07)
        assert years is not None
        assert 10.0 < years < 10.6

    def test_unknown_rate_is_unavailable(self):
        assert goal_service._years_to_target(10_000, 100_000, 5_000, None) is None

    def test_degenerate_rate_is_unavailable(self):
        assert goal_service._years_to_target(10_000, 100_000, 5_000, -1.0) is None
        assert goal_service._years_to_target(10_000, 100_000, 5_000, -1.5) is None

    def test_negative_rate_still_reaches_target_via_contribution(self):
        # Balance shrinks under a -5% drag, but a $30k/yr contribution outpaces it.
        years = goal_service._years_to_target(50_000, 200_000, 30_000, -0.05)
        assert years is not None
        assert years < goal_service._CAP_YEARS

    def test_unreachable_within_horizon_is_none(self):
        # Negative rate, zero contribution: the balance only shrinks — never gets there.
        assert goal_service._years_to_target(50_000, 200_000, 0, -0.10) is None


# ── golden facets — synthetic PerfPoint series, no DB ────────────────────────


def _points(navs: list[tuple[date, float]]) -> list[performance.PerfPoint]:
    return [performance.PerfPoint(snap_date=d, nav=nav, external_flow=0.0, spy_close=None) for d, nav in navs]


def _summary(points: list[performance.PerfPoint], *, twr=None, mwr=None, ann_twr=None, ann_mwr=None):
    s = performance.PerformanceSummary(n_snapshots=len(points), points=points)
    if points:
        s.first_date = points[0].snap_date
        s.last_date = points[-1].snap_date
        s.latest_nav = points[-1].nav
        s.days = (points[-1].snap_date - points[0].snap_date).days
    s.twr = twr
    s.mwr = mwr
    s.annualized_twr = ann_twr
    s.annualized_mwr = ann_mwr
    return s


class TestProgress:
    def test_no_goal_set(self):
        r = goal_service.progress(None, 50_000, [])
        assert r == {"available": False, "as_of": date.today().isoformat(), "reason": "no_goal_set"}

    def test_progress_ratio_and_period_change(self):
        goal = models.RetirementGoal(target_amount_usd=200_000)
        today = date(2026, 9, 1)
        pts = _points([(today - timedelta(days=400), 80_000), (today, 100_000)])
        r = goal_service.progress(goal, 100_000, pts)
        assert r["available"] is True
        assert r["progress_ratio"] == pytest.approx(0.5)
        # prior ratio 80000/200000=0.4, now 0.5 -> +0.1
        assert r["period_change_ratio"] == pytest.approx(0.1)

    def test_no_prior_snapshot_means_no_period_change(self):
        goal = models.RetirementGoal(target_amount_usd=200_000)
        r = goal_service.progress(goal, 100_000, _points([(date(2026, 9, 1), 100_000)]))
        assert r["available"] is True
        assert r["period_change_ratio"] is None


class TestTrajectoryRange:
    def test_no_goal_set(self):
        r = goal_service.trajectory_range(None, 50_000, _summary([]))
        assert r["available"] is False and r["reason"] == "no_goal_set"

    def test_negative_since_inception_twr_still_computes_via_contribution(self):
        goal = models.RetirementGoal(target_amount_usd=500_000, annual_contribution_usd=40_000)
        end = date(2026, 9, 1)
        pts = _points([(end - timedelta(days=800), 300_000), (end, 250_000)])
        summary = _summary(pts, ann_twr=-0.05)
        r = goal_service.trajectory_range(goal, 250_000, summary)
        assert r["available"] is True
        assert r["years_low"] <= r["years_high"]
        assert r["rates"]["since_inception"] == -0.05

    def test_zero_contribution_and_no_measurable_rate_is_unavailable(self):
        goal = models.RetirementGoal(target_amount_usd=500_000, annual_contribution_usd=0)
        r = goal_service.trajectory_range(goal, 250_000, _summary(_points([(date(2026, 9, 1), 250_000)])))
        assert r["available"] is False
        assert r["reason"] == "insufficient_history_or_unreachable"

    def test_no_valuation_is_unavailable(self):
        goal = models.RetirementGoal(target_amount_usd=500_000)
        r = goal_service.trajectory_range(goal, None, _summary([]))
        assert r["available"] is False and r["reason"] == "no_valuation"


class TestTimingCost:
    def test_no_goal_set(self):
        r = goal_service.timing_cost_years(None, 50_000, _summary([]))
        assert r["available"] is False and r["reason"] == "no_goal_set"

    def test_insufficient_history_when_annualized_missing(self):
        goal = models.RetirementGoal(target_amount_usd=500_000, annual_contribution_usd=10_000)
        r = goal_service.timing_cost_years(goal, 100_000, _summary(_points([(date(2026, 9, 1), 100_000)])))
        assert r["available"] is False and r["reason"] == "insufficient_history"

    def test_mwr_below_twr_adds_years(self):
        goal = models.RetirementGoal(target_amount_usd=300_000, annual_contribution_usd=10_000)
        summary = _summary(
            _points([(date(2024, 1, 1), 100_000), (date(2026, 9, 1), 150_000)]),
            ann_twr=0.10,
            ann_mwr=0.04,  # poor timing: MWR well below TWR
        )
        r = goal_service.timing_cost_years(goal, 150_000, summary)
        assert r["available"] is True
        assert r["gap_pct"] == pytest.approx(-0.06)
        assert r["years_added"] > 0  # the poorer MWR rate takes longer to reach the goal


class TestFeeDrag:
    def test_always_unavailable(self):
        r = goal_service.fee_drag()
        assert r["available"] is False
        assert r["reason"] == "expense_ratio_source_not_provisioned"


# ── goal_observation_* producer contract (for api.services.glance, metron-PR460) ────


class TestGoalObservations:
    def test_registry_has_all_six_facet_keys(self):
        assert set(goal_service.GOAL_OBSERVATIONS) == {
            "goal_progress",
            "goal_trajectory_range",
            "goal_timing_cost",
            "goal_fee_drag",
            "goal_asset_location_drag",
            "goal_withdrawal_readiness",
        }

    def test_fee_drag_producer_is_always_empty(self, db_session):
        tenant_id = uuid.uuid4()
        p = _make_portfolio(db_session, tenant_id)
        out = goal_service.goal_observation_goal_fee_drag(db_session, tenant_id, p.id, "2026-09-15")
        assert out == []

    def test_no_goal_set_producers_return_empty(self, db_session):
        tenant_id = uuid.uuid4()
        p = _make_portfolio(db_session, tenant_id)
        for key, fn in goal_service.GOAL_OBSERVATIONS.items():
            assert fn(db_session, tenant_id, p.id, "2026-09-15") == [], key

    def test_progress_producer_shape_once_goal_and_history_exist(self, db_session):
        tenant_id = uuid.uuid4()
        p = _make_portfolio(db_session, tenant_id)
        goal_service.set_goal(
            db_session,
            tenant_id,
            p.id,
            target_amount_usd=200_000,
            target_date=None,
            annual_contribution_usd=10_000,
            withdrawal_rate=None,
        )
        _make_account(db_session, tenant_id, p.id, tax_treatment="taxable")
        db_session.add(
            models.NavSnapshot(
                tenant_id=tenant_id,
                portfolio_id=p.id,
                snap_date=date.today() - timedelta(days=400),
                nav=80_000,
                external_flow=0,
                cost_basis=80_000,
            )
        )
        db_session.add(
            models.NavSnapshot(
                tenant_id=tenant_id,
                portfolio_id=p.id,
                snap_date=date.today(),
                nav=100_000,
                external_flow=0,
                cost_basis=80_000,
            )
        )
        db_session.commit()
        out = goal_service.goal_observation_goal_progress(db_session, tenant_id, p.id, "2026-09-15")
        assert len(out) == 1
        obs = out[0]
        assert obs["facet_key"] == "goal_progress"
        assert obs["as_of"] == "2026-09-15"  # the CALLER's as_of, not the recomputed one
        assert obs["surface"] == "overview"
        assert obs["value"] == pytest.approx(0.5)
        assert 0.0 <= obs["materiality"] <= 1.0
        assert isinstance(obs["text"], str) and obs["text"]


# ── DB-backed facets: asset-location drag + withdrawal readiness ────────────


def _make_portfolio(db_session, tenant_id):
    p = models.Portfolio(tenant_id=tenant_id, name="P")
    db_session.add(models.Tenant(id=tenant_id, name="T"))
    db_session.add(p)
    db_session.commit()
    return p


def _make_account(db_session, tenant_id, portfolio_id, *, tax_treatment, cash=0.0, external_id="a1"):
    a = models.Account(
        tenant_id=tenant_id,
        portfolio_id=portfolio_id,
        broker="csv",
        external_id=external_id,
        tax_treatment=tax_treatment,
        cash_balance_usd=cash,
    )
    db_session.add(a)
    db_session.commit()
    return a


def _make_dividend(db_session, tenant_id, account_id, amount, when, key):
    t = models.Transaction(
        tenant_id=tenant_id,
        account_id=account_id,
        txn_type="DIVIDEND",
        amount=amount,
        trade_date=when,
        source_key=key,
    )
    db_session.add(t)
    db_session.commit()


class TestAssetLocationDrag:
    def test_no_accounts(self, db_session):
        tenant_id = uuid.uuid4()
        p = _make_portfolio(db_session, tenant_id)
        r = goal_service.asset_location_drag(db_session, tenant_id, p.id)
        assert r["available"] is False and r["reason"] == "no_accounts"

    def test_splits_income_taxable_vs_sheltered_and_never_invents_a_rate(self, db_session):
        tenant_id = uuid.uuid4()
        p = _make_portfolio(db_session, tenant_id)
        taxable = _make_account(db_session, tenant_id, p.id, tax_treatment="taxable", external_id="tax")
        sheltered = _make_account(db_session, tenant_id, p.id, tax_treatment="tax_deferred", external_id="ira")
        today = date.today()
        _make_dividend(db_session, tenant_id, taxable.id, 500.0, today - timedelta(days=10), "k1")
        _make_dividend(db_session, tenant_id, sheltered.id, 200.0, today - timedelta(days=10), "k2")
        r = goal_service.asset_location_drag(db_session, tenant_id, p.id)
        assert r["available"] is True
        assert r["taxable_dividend_interest_income_usd"] == pytest.approx(500.0)
        assert r["sheltered_dividend_interest_income_usd"] == pytest.approx(200.0)
        assert r["assumed_tax_rate"] is None
        assert r["estimated_tax_usd_per_year"] is None

    def test_excludes_income_older_than_trailing_window(self, db_session):
        tenant_id = uuid.uuid4()
        p = _make_portfolio(db_session, tenant_id)
        taxable = _make_account(db_session, tenant_id, p.id, tax_treatment="taxable")
        _make_dividend(db_session, tenant_id, taxable.id, 900.0, date.today() - timedelta(days=800), "old")
        r = goal_service.asset_location_drag(db_session, tenant_id, p.id)
        assert r["taxable_dividend_interest_income_usd"] == 0.0


class TestWithdrawalReadiness:
    def test_no_withdrawal_rate_set(self, db_session):
        tenant_id = uuid.uuid4()
        p = _make_portfolio(db_session, tenant_id)
        goal = models.RetirementGoal()
        r = goal_service.withdrawal_readiness(db_session, tenant_id, p.id, goal, 100_000)
        assert r["available"] is False and r["reason"] == "no_withdrawal_rate_set"

    def test_months_covered_per_account_type(self, db_session):
        tenant_id = uuid.uuid4()
        p = _make_portfolio(db_session, tenant_id)
        taxable = _make_account(db_session, tenant_id, p.id, tax_treatment="taxable", cash=6_000, external_id="tax")
        _make_dividend(db_session, tenant_id, taxable.id, 6_000.0, date.today() - timedelta(days=5), "d1")
        goal = models.RetirementGoal(withdrawal_rate=0.04)
        # current_value=300000 * 4% = 12000/yr = 1000/mo. taxable has 6000 cash + 6000
        # income = 12000 available -> 12.0 months.
        r = goal_service.withdrawal_readiness(db_session, tenant_id, p.id, goal, 300_000)
        assert r["available"] is True
        assert r["monthly_withdrawal_usd"] == pytest.approx(1_000.0)
        assert r["by_account_type"]["taxable"]["months_covered"] == pytest.approx(12.0)


# ── entitlement + registry contract ──────────────────────────────────────────


def test_goal_feature_registered_and_in_beta_tier():
    assert "goal" in entitlements.FEATURE_BY_KEY
    assert "goal" in entitlements.TIER_BY_KEY["beta"].features
    resolved = entitlements.resolve("beta", feed_enabled=False)
    goal_state = next(f for f in resolved["features"] if f["key"] == "goal")
    assert goal_state["available"] is True


def test_six_goal_facets_registered_l1():
    goal_facets = [f for f in CATALOG if f.feature == "goal"]
    assert len(goal_facets) == 6
    keys = {f.key for f in goal_facets}
    assert keys == {
        "goal_progress",
        "goal_trajectory_range",
        "goal_timing_cost",
        "goal_fee_drag",
        "goal_asset_location_drag",
        "goal_withdrawal_readiness",
    }
    for f in goal_facets:
        assert f.level == "L1"
        assert f.surface == "overview"


def test_goal_facet_keys_resolve_in_family_by_key():
    for key in ("goal_progress", "goal_trajectory_range"):
        assert FACET_BY_KEY[key].family == "performance"
    assert FACET_BY_KEY["goal_timing_cost"].family == "behaviour"
    assert FACET_BY_KEY["goal_asset_location_drag"].family == "structure"


# ── router ────────────────────────────────────────────────────────────────────


@pytest.fixture()
def tenant():
    return str(uuid.uuid4())


def _seed_portfolio(client, tenant):
    return client.post("/portfolios", json={"name": "P"}, headers={"X-Tenant-Id": tenant}).json()["id"]


class TestGoalRouter:
    def test_get_before_set_is_empty_no_prefill(self, client, tenant):
        pid = _seed_portfolio(client, tenant)
        r = client.get(f"/portfolios/{pid}/goal", headers={"X-Tenant-Id": tenant})
        assert r.status_code == 200
        body = r.json()
        assert body == {
            "target_amount_usd": None,
            "target_date": None,
            "annual_contribution_usd": None,
            "withdrawal_rate": None,
        }

    def test_facets_before_goal_set_all_degrade_with_reason(self, client, tenant):
        pid = _seed_portfolio(client, tenant)
        r = client.get(f"/portfolios/{pid}/goal/facets", headers={"X-Tenant-Id": tenant})
        assert r.status_code == 200
        body = r.json()
        assert set(body) == {
            "goal_progress",
            "goal_trajectory_range",
            "goal_timing_cost",
            "goal_fee_drag",
            "goal_asset_location_drag",
            "goal_withdrawal_readiness",
        }
        for key, payload in body.items():
            assert payload["available"] is False, key
            assert payload.get("reason") or key == "goal_asset_location_drag"

    def test_put_then_get_round_trips(self, client, tenant):
        pid = _seed_portfolio(client, tenant)
        put = client.put(
            f"/portfolios/{pid}/goal",
            json={
                "target_amount_usd": 1_500_000,
                "target_date": "2045-01-01",
                "annual_contribution_usd": 25_000,
                "withdrawal_rate": 0.04,
            },
            headers={"X-Tenant-Id": tenant},
        )
        assert put.status_code == 200
        got = client.get(f"/portfolios/{pid}/goal", headers={"X-Tenant-Id": tenant}).json()
        assert got["target_amount_usd"] == 1_500_000
        assert got["target_date"] == "2045-01-01"
        assert got["annual_contribution_usd"] == 25_000
        assert got["withdrawal_rate"] == 0.04

    def test_cross_tenant_404(self, client, tenant):
        pid = _seed_portfolio(client, tenant)
        other = str(uuid.uuid4())
        r = client.get(f"/portfolios/{pid}/goal", headers={"X-Tenant-Id": other})
        assert r.status_code == 404

    def test_unknown_field_rejected(self, client, tenant):
        pid = _seed_portfolio(client, tenant)
        r = client.put(
            f"/portfolios/{pid}/goal",
            json={"target_amount_usd": 100, "suggested_target": 999},
            headers={"X-Tenant-Id": tenant},
        )
        assert r.status_code == 422


# ── migration round-trip (structural) ────────────────────────────────────────


def test_retirement_goal_migration_upgrade_downgrade_are_symmetric():
    """The hand-written revision creates exactly the table its downgrade drops —
    mirrors the b7d3e91a4c2f/a1f2c3d4e5b6 precedent this migration follows. A full
    DDL round-trip against a live engine is the Postgres CI job's job (see
    tests/test_migrations_cover_models.py's docstring on why SQLite can't run
    Postgres-flavoured DDL like ``server_default=sa.text('now()')``)."""
    import pathlib
    import re

    versions = pathlib.Path(__file__).resolve().parent.parent / "alembic" / "versions"
    migration = next(versions.glob("c4e8f2a9d6b1_*.py"))
    src = migration.read_text(encoding="utf-8")
    # Chained onto the plan_targets migration (metron-PR463); the graph shape itself is
    # asserted by tests/test_alembic_single_head.py rather than a hardcoded parent id.
    assert re.search(r"down_revision.*c9e4f27a1b83", src)
    created = set(re.findall(r"op\.create_table\(\s*['\"]([a-z_0-9]+)['\"]", src))
    dropped = set(re.findall(r"op\.drop_table\(\s*['\"]([a-z_0-9]+)['\"]", src))
    assert created == {"retirement_goal"}
    assert dropped == {"retirement_goal"}


def test_retirement_goal_table_present_in_model_metadata():
    """Round-trip via create_all (the repo's SQLite-testable equivalent — see
    tests/test_migrations.py's account_nav_snapshots precedent)."""
    from sqlalchemy import create_engine, inspect

    from api.db.session import Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    assert "retirement_goal" in set(inspect(engine).get_table_names())
    engine.dispose()
