"""Glance screen composition + endpoint (metron-ops#248 Stage A, #250, #214).

Covers: all six zones present in one payload; the render state as a field; per-row
provenance and as-of on every ranked item; the quiet-day floor in every ranked zone;
integrity rendered on every path (healthy included); feed-off degradation to "not
available"; generic rendering of any facet with a producer; producer failure isolation;
entitlement; ownership; server timing; and a contract check that the headline matches
``GET /summary`` for the same portfolio.

The clock is PINNED — nothing here reads wall time for a decision.
"""

from __future__ import annotations

import io
import logging
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest

from api import entitlements as ent
from api.config import settings
from api.db import models
from api.insights import ranker, registry
from api.services import glance
from api.services import goal as goal_service
from portfolio_analytics.prices import ClosePoint

_POST_CLOSE = datetime(2026, 9, 15, 22, 0, tzinfo=UTC)  # Tue 18:00 ET


# ── fixtures ──────────────────────────────────────────────────────────────────


def _portfolio(session, *, broker: str | None = None) -> models.Portfolio:
    tid = uuid.uuid4()
    session.add(models.Tenant(id=tid, name="t"))
    p = models.Portfolio(tenant_id=tid, name="P", base_currency="USD")
    session.add(p)
    session.flush()
    if broker:
        session.add(models.Account(tenant_id=tid, portfolio_id=p.id, broker=broker, external_id="A1", name="A"))
    session.commit()
    return p


def _leg(ticker, qty, price):
    return {"ticker": ticker, "qty": qty, "price": price, "fx_rate": 1.0, "currency": "USD", "value": qty * price}


def _snap(session, p, when, legs):
    session.add(
        models.NavSnapshot(
            tenant_id=p.tenant_id,
            portfolio_id=p.id,
            snap_date=when,
            nav=sum(leg["value"] for leg in legs),
            cost_basis=0,
            external_flow=0,
            composition={"schema": 1, "legs": legs},
        )
    )
    session.commit()


def _compose(session, p, **kw):
    kw.setdefault("tier", "personal")
    kw.setdefault("feed_enabled", True)
    kw.setdefault("now", _POST_CLOSE)
    return glance.compose(session, p, **kw)


# ── render state ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("now", "state"),
    [
        (datetime(2026, 9, 15, 13, 0, tzinfo=UTC), glance.STATE_PRE_OPEN),  # 09:00 ET
        (datetime(2026, 9, 15, 15, 0, tzinfo=UTC), glance.STATE_OPEN),  # 11:00 ET
        (_POST_CLOSE, glance.STATE_POST_CLOSE),
        (datetime(2026, 9, 19, 15, 0, tzinfo=UTC), glance.STATE_POST_CLOSE),  # Saturday
    ],
)
def test_render_state(now, state):
    assert glance.render_state(now) == state


# ── zone composition ─────────────────────────────────────────────────────────


def test_movers_rank_by_dollar_contribution_from_settled_snapshots(db_session):
    p = _portfolio(db_session)
    _snap(db_session, p, date(2026, 9, 14), [_leg("AAPL", 10, 100), _leg("XOM", 5, 100), _leg("KO", 1, 50)])
    _snap(db_session, p, date(2026, 9, 15), [_leg("AAPL", 10, 110), _leg("XOM", 5, 98), _leg("KO", 1, 50.1)])

    g = _compose(db_session, p)

    movers = g.movers
    assert not movers.is_floor
    assert [i.ticker for i in movers.items] == ["AAPL", "XOM"]  # KO's $0.10 is below the floor
    assert movers.items[0].amount == pytest.approx(100.0)
    assert movers.items[1].amount == pytest.approx(-10.0)
    for item in movers.items:
        assert item.provenance == glance.PROV_SETTLED
        assert item.as_of == "2026-09-15"
        assert item.surface == "overview"
    # The headline's day change is the same settled TODAY window the Overview tile uses.
    assert g.headline.day_provenance == glance.PROV_SETTLED
    assert g.headline.day_as_of == "2026-09-15"
    assert g.headline.day_change == pytest.approx(90.1)
    assert g.path.available and g.path.points[-1].date == "2026-09-15"


def test_quiet_day_floor_in_every_ranked_zone(db_session):
    p = _portfolio(db_session)
    _snap(db_session, p, date(2026, 9, 14), [_leg("AAPL", 10, 100)])
    _snap(db_session, p, date(2026, 9, 15), [_leg("AAPL", 10, 100.01)])

    g = _compose(db_session, p)

    assert g.movers.is_floor and g.movers.floor_text == glance.MOVERS_FLOOR_TEXT and g.movers.items == []
    assert g.insights.is_floor and g.insights.floor_text == ranker.FLOOR_TEXT
    assert g.ahead.is_floor and g.ahead.floor_text == glance.AHEAD_FLOOR_TEXT
    for zone in (g.movers, g.insights, g.ahead):
        assert zone.as_of  # the floor is a claim about a moment — never unprovenanced


def test_every_zone_present_on_an_empty_portfolio(db_session):
    p = _portfolio(db_session)
    g = _compose(db_session, p)
    assert g.state == glance.STATE_POST_CLOSE
    assert g.headline.total_value is None
    assert g.headline.day_note  # an honest reason, not a zero
    assert not g.path.available and g.path.reason
    assert g.integrity.status == "no_accounts" and g.integrity.text


# ── integrity: always rendered ───────────────────────────────────────────────


def test_integrity_ledger_only_is_rendered_and_healthy(db_session):
    p = _portfolio(db_session, broker="csv")
    i = _compose(db_session, p).integrity
    assert i.status == "ledger_only" and i.healthy and i.text


def test_integrity_never_reconciled(db_session):
    p = _portfolio(db_session, broker="snaptrade")
    i = _compose(db_session, p).integrity
    assert i.status == "never" and not i.healthy


def _fetch(session, p, *, success_at):
    session.add(
        models.ReconciliationFetchStatus(
            tenant_id=p.tenant_id, portfolio_id=p.id, broker="snaptrade",
            last_success_at=success_at, first_seen_at=success_at,
        )
    )
    session.commit()


def test_integrity_healthy_path_says_reconciled_with_its_time(db_session):
    p = _portfolio(db_session, broker="snaptrade")
    _fetch(db_session, p, success_at=(_POST_CLOSE - timedelta(hours=1)).replace(tzinfo=None))
    i = _compose(db_session, p).integrity
    assert i.status == "reconciled" and i.healthy
    assert i.text == "Reconciled as of Sep 15, 17:00 ET."
    assert i.last_reconciled_at is not None


def test_integrity_stale(db_session):
    p = _portfolio(db_session, broker="snaptrade")
    old = _POST_CLOSE - timedelta(hours=settings.reconciliation_stale_hours + 1)
    _fetch(db_session, p, success_at=old.replace(tzinfo=None))
    i = _compose(db_session, p).integrity
    assert i.status == "stale" and not i.healthy


def test_integrity_open_breaks(db_session):
    p = _portfolio(db_session, broker="snaptrade")
    _fetch(db_session, p, success_at=(_POST_CLOSE - timedelta(hours=1)).replace(tzinfo=None))
    acct = db_session.query(models.Account).filter_by(portfolio_id=p.id).one()
    db_session.add(
        models.ReconciliationBreak(
            tenant_id=p.tenant_id, account_id=acct.id, break_type="cash", break_key="k1",
            break_date=date(2026, 9, 15),
        )
    )
    db_session.commit()
    i = _compose(db_session, p).integrity
    assert i.status == "breaks" and i.open_breaks == 1 and not i.healthy


# ── entitlement / feed degradation ───────────────────────────────────────────


def test_glance_is_in_the_beta_tier_without_the_feed():
    feats = {f["key"]: f for f in ent.resolve("beta", feed_enabled=False)["features"]}
    assert feats["glance"]["available"]


def test_feed_off_names_feed_facets_unavailable_and_never_computes_them(db_session, monkeypatch):
    called = []
    monkeypatch.setattr(glance, "PRODUCERS", {**glance.PRODUCERS, "upcoming_earnings": lambda ctx: called.append(1) or []})
    p = _portfolio(db_session)

    off = _compose(db_session, p, tier="beta", feed_enabled=False)
    assert "Upcoming earnings for your holdings" in off.ahead.unavailable
    assert called == []
    assert off.feed_enabled is False

    on = _compose(db_session, p, tier="personal", feed_enabled=True)
    assert "Upcoming earnings for your holdings" not in on.ahead.unavailable
    assert called == [1]


def test_any_registered_facet_with_a_producer_renders(db_session, monkeypatch):
    """Zones render whatever the ranker yields — a facet this module has no special
    handling for appears with its registry label and tap-through surface."""
    monkeypatch.setattr(glance, "PRODUCERS", dict(glance.PRODUCERS))

    @glance.register_producer("fee_drag")
    def _fee(ctx):
        return [glance.Candidate(ranker.Observation(facet_key="fee_drag", text="Blended fund expense is $120 a year.",
                                                    as_of="2026-09-15", materiality=0.9), provenance=glance.PROV_AS_OF_CLOSE)]

    p = _portfolio(db_session)
    g = _compose(db_session, p)
    item = next(i for i in g.insights.items if i.facet_key == "fee_drag")
    assert item.label == registry.FACET_BY_KEY["fee_drag"].label
    assert item.surface == "holdings"
    assert item.as_of == "2026-09-15"
    assert not g.insights.is_floor


def test_unknown_family_renders_in_insights():
    facet = registry.Facet(key="goal_progress", family="goal", label="Goal", requires=("broker",),
                           feature="overview", surface="settings")
    assert glance.zone_for(facet) == glance.ZONE_INSIGHTS


def test_register_producer_rejects_an_unregistered_facet():
    with pytest.raises(ValueError):
        glance.register_producer("not_a_facet")(lambda ctx: [])


def test_a_failing_producer_degrades_its_facet_not_the_screen(db_session, monkeypatch, caplog):
    def _boom(ctx):
        raise RuntimeError("boom")

    monkeypatch.setattr(glance, "PRODUCERS", {**glance.PRODUCERS, "cash_drag": _boom})
    p = _portfolio(db_session, broker="csv")
    with caplog.at_level(logging.ERROR, logger="api.services.glance"):
        g = _compose(db_session, p)
    assert g.degraded == ["cash_drag"]
    assert g.integrity.text
    assert "glance producer failed facet=cash_drag" in caplog.text


# ── goal producers (metron-ops-I320) ────────────────────────────────────────


def test_goal_observation_keys_all_have_registered_producers():
    """Contract test: a key added to ``goal.GOAL_OBSERVATIONS`` without a matching
    registered glance producer would silently never render — the loop in this module
    wires one per key at import time (which itself raises immediately via
    ``register_producer`` for an unregistered facet), so this additionally guards the
    steady-state invariant after any future edit to either module."""
    assert set(goal_service.GOAL_OBSERVATIONS) <= set(glance.PRODUCERS)


def test_goal_set_shows_goal_progress_with_its_as_of(db_session):
    p = _portfolio(db_session)
    goal_service.set_goal(
        db_session, p.tenant_id, p.id,
        target_amount_usd=200_000, target_date=None, annual_contribution_usd=10_000, withdrawal_rate=None,
    )
    _snap(db_session, p, date(2025, 9, 14), [_leg("AAPL", 800, 100)])  # nav 80,000
    _snap(db_session, p, date(2026, 9, 15), [_leg("AAPL", 1000, 100)])  # nav 100,000

    g = _compose(db_session, p)

    item = next(i for i in g.insights.items if i.facet_key == "goal_progress")
    assert item.as_of == "2026-09-15"
    assert item.surface == "overview"
    assert item.provenance == glance.PROV_AS_OF_CLOSE
    assert g.degraded == []


def test_no_goal_set_yields_no_goal_candidates_and_nothing_degraded(db_session):
    p = _portfolio(db_session)
    _snap(db_session, p, date(2025, 9, 14), [_leg("AAPL", 800, 100)])
    _snap(db_session, p, date(2026, 9, 15), [_leg("AAPL", 1000, 100)])

    g = _compose(db_session, p)

    all_items = [*g.movers.items, *g.insights.items, *g.ahead.items]
    assert not any(i.facet_key.startswith("goal_") for i in all_items)
    assert g.degraded == []


# ── endpoint ─────────────────────────────────────────────────────────────────

CSV = "date,type,symbol,quantity,price\n2024-01-01,BUY,AAPL,10,100\n2024-01-01,BUY,XOM,5,100\n"


def _latest(symbols, *, source=None):
    return {s: ClosePoint(date(2024, 2, 19), 120.0) for s in symbols if s in {"AAPL", "XOM"}}


def _seed(client, monkeypatch) -> tuple[str, str]:
    tenant = str(uuid.uuid4())
    h = {"X-Tenant-Id": tenant}
    pid = client.post("/portfolios", json={"name": "P"}, headers=h).json()["id"]
    assert client.post(
        f"/portfolios/{pid}/import/csv", files={"file": ("t.csv", io.BytesIO(CSV.encode()), "text/csv")}, headers=h
    ).status_code == 200
    monkeypatch.setattr("api.services.prices.fetch_latest_closes", _latest)
    monkeypatch.setattr("api.services.performance.fetch_latest_closes", lambda s, *, source=None: {})
    client.post(f"/portfolios/{pid}/prices/refresh", headers=h)
    return tenant, pid


def test_endpoint_returns_all_zones_in_one_payload(client, monkeypatch, caplog):
    tenant, pid = _seed(client, monkeypatch)
    with caplog.at_level(logging.INFO, logger="api.routers.glance"):
        r = client.get(f"/portfolios/{pid}/glance", headers={"X-Tenant-Id": tenant})
    assert r.status_code == 200, r.text
    g = r.json()
    for zone in ("headline", "path", "movers", "insights", "ahead", "integrity"):
        assert zone in g
    assert g["state"] in {"pre_open", "open", "post_close"}
    assert g["integrity"]["text"]
    for zone in ("movers", "insights", "ahead"):
        for item in g[zone]["items"]:
            assert item["as_of"] and item["provenance"] and item["surface"]
    assert r.headers["Server-Timing"].startswith("glance;dur=")
    assert "glance composed" in caplog.text and "duration_ms=" in caplog.text

    # Contract (metron-ops#250): the aggregate's numbers match the individual service's.
    s = client.get(f"/portfolios/{pid}/summary", headers={"X-Tenant-Id": tenant}).json()
    assert g["headline"]["market_value"] == pytest.approx(s["market_value"])
    assert g["headline"]["total_value"] == pytest.approx((s["market_value"] or 0) + (s["cash"] or 0))
    assert g["headline"]["value_as_of"] == "2024-02-19"
    # AAPL is 50% of invested value → the concentration fact clears the floor.
    assert any(i["facet_key"] == "concentration_top_weight" for i in g["insights"]["items"])


def test_endpoint_is_owner_scoped(client, monkeypatch):
    _, pid = _seed(client, monkeypatch)
    r = client.get(f"/portfolios/{pid}/glance", headers={"X-Tenant-Id": str(uuid.uuid4())})
    assert r.status_code == 404


def test_endpoint_honours_the_simulator_feed_preview(client, monkeypatch):
    tenant, pid = _seed(client, monkeypatch)
    monkeypatch.setattr(settings, "tier_simulator", True)
    r = client.get(
        f"/portfolios/{pid}/glance", headers={"X-Tenant-Id": tenant, "X-Preview-Tier": "beta", "X-Preview-Feed": "false"}
    )
    g = r.json()
    assert g["feed_enabled"] is False and g["tier"] == "beta"
    assert "Upcoming earnings for your holdings" in g["ahead"]["unavailable"]


def test_endpoint_refuses_when_the_tier_excludes_glance(client, monkeypatch):
    tenant, pid = _seed(client, monkeypatch)
    monkeypatch.setattr(ent, "feature_state", lambda *a, **k: {"available": False, "reason": "tier"})
    r = client.get(f"/portfolios/{pid}/glance", headers={"X-Tenant-Id": tenant})
    assert r.status_code == 403
