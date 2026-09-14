"""Per-holding tearsheet (metron-ops#22) — Position + Performance + Technical from data
Metron already has; the fundamentals blocks are honestly N/A until the spine artifact ships.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest

from api.db import models
from api.services import security_performance, tearsheet


def _seed(session):
    tenant = models.Tenant(name="t")
    session.add(tenant)
    session.flush()
    pf = models.Portfolio(tenant_id=tenant.id, name="P", base_currency="USD")
    session.add(pf)
    session.flush()
    acct = models.Account(tenant_id=tenant.id, portfolio_id=pf.id, broker="csv", external_id="CSV-1", currency="USD")
    aapl = models.Security(symbol="AAPL", currency="USD")
    spy = models.Security(symbol="SPY", currency="USD")
    session.add_all([acct, aapl, spy])
    session.flush()
    session.add(
        models.Transaction(
            tenant_id=tenant.id, account_id=acct.id, security_id=aapl.id, txn_type="BUY",
            quantity=10, price=100.0, amount=1000.0, currency="USD",
            trade_date=date(2025, 1, 1), source_key="buy-aapl",
        )
    )
    start = date(2025, 1, 1)
    av, sv = 100.0, 400.0
    for i in range(70):
        av *= 1.01 if i % 2 == 0 else 0.995
        sv *= 1.008 if i % 2 == 0 else 0.997
        d = start + timedelta(days=i)
        session.add(models.PriceBar(security_id=aapl.id, bar_date=d, close=av, currency="USD"))
        session.add(models.PriceBar(security_id=spy.id, bar_date=d, close=sv, currency="USD"))
    session.commit()
    return tenant.id, pf.id


_PERF_ART = {
    "as_of": "2026-06-26",
    "performance": {
        "AAPL": {
            "period_returns": {"1Y": 0.12},
            "ytd_pct": 0.08,
            "ltm_pct": 0.10,
            "volatility": 0.22,
            "sharpe": 1.1,
            "sortino": 1.4,
            "max_drawdown": -0.09,
            "beta_vs_spy": 1.05,
            "vs_spy_window": 0.02,
            "vs_spy_1y": 0.03,
            "n_bars": 252,
            "history_from": "2025-01-01",
        },
    },
}

_TECH_ART = {
    "as_of": "2026-06-26",
    "technicals": {
        "AAPL": {
            "rsi_14": 62.0,
            "pct_from_52wk_high": -0.04,
        },
    },
}


def test_tearsheet_position_and_performance(db_session):
    tenant_id, pid = _seed(db_session)
    sheet = tearsheet.tearsheet(
        db_session, tenant_id, pid, "AAPL",
        feed_enabled=True,
        performance_reader=lambda: _PERF_ART,
        technicals_reader=lambda: _TECH_ART,
    )
    assert sheet is not None
    assert sheet.position.ticker == "AAPL"
    assert sheet.position.quantity == 10
    assert sheet.position.cost_basis == pytest.approx(1000.0)
    assert sheet.position.market_value is not None and sheet.position.market_value > 1000.0
    assert sheet.position.unrealized_pct is not None and sheet.position.unrealized_pct > 0
    assert sheet.position.weight_pct == pytest.approx(1.0)
    assert sheet.position.accounts == ["CSV-1"]
    # Performance from spine artifact (not local price_bars).
    assert sheet.performance.n_bars == 252
    assert sheet.performance.return_vs_cost is not None
    assert sheet.performance.volatility == pytest.approx(0.22)
    assert sheet.performance.sharpe == pytest.approx(1.1)
    assert sheet.performance.sortino == pytest.approx(1.4)
    assert sheet.performance.max_drawdown == pytest.approx(-0.09)
    assert sheet.performance.beta_vs_spy == pytest.approx(1.05)
    assert sheet.performance.vs_spy == pytest.approx(0.02)
    assert sheet.performance.vs_spy_1y == pytest.approx(0.03)
    assert sheet.technical.rsi_14 == pytest.approx(62.0)
    assert sheet.technical.pct_from_52wk_high == pytest.approx(-0.04)
    assert sheet.technical.forward_div_yield is None
    assert sheet.fundamentals_available is False
    assert "1022" in sheet.fundamentals_reason


def test_tearsheet_off_feed_shows_position_only(db_session):
    tenant_id, pid = _seed(db_session)
    sheet = tearsheet.tearsheet(db_session, tenant_id, pid, "AAPL", feed_enabled=False)
    assert sheet.performance.return_vs_cost is not None
    assert sheet.performance.n_bars == 0
    assert sheet.performance.volatility is None
    assert sheet.technical.rsi_14 is None
    # Technical rating (metron-ops#294) — off-feed omits it exactly like every other
    # spine-derived metric.
    assert sheet.technical.tech_rating_score is None
    assert sheet.technical.tech_rating_label is None


# ── Technical rating (metron-ops#294) ────────────────────────────────────────────────

_RATING_ART_FRESH = {
    "schema_version": 1,
    "as_of_utc": "2026-06-26T14:55:00Z",
    "quote_as_of_utc": "2026-06-26T14:55:00Z",
    "source": "computed_intraday",
    "ratings": {
        "AAPL": {
            "score": 0.6, "label": "Buy", "ma_score": 0.7, "osc_score": 0.5,
            "n_buy": 8, "n_neutral": 2, "n_sell": 1, "n_votes": 11,
            "price": 227.5, "bar_date": "2026-06-26", "basis": "intraday",
        },
    },
}

_TECH_ART_WITH_RATING = {
    **_TECH_ART,
    "technicals": {
        "AAPL": {
            **_TECH_ART["technicals"]["AAPL"],
            "rating": {
                "score": -0.2, "label": "Sell", "ma_score": -0.1, "osc_score": -0.3,
                "n_buy": 2, "n_neutral": 3, "n_sell": 6, "n_votes": 11,
            },
        },
    },
}


def test_tearsheet_technical_rating_uses_fresh_intraday(db_session):
    # _RATING_ART_FRESH's as_of_utc is a fixed 2026-06-26 timestamp; the reader is only
    # "fresh" relative to the real wall clock ``tearsheet()`` uses (now=None), so pin the
    # artifact's as_of to "now" here rather than depending on when this test runs.
    from datetime import UTC, datetime

    art = {**_RATING_ART_FRESH, "as_of_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z")}
    tenant_id, pid = _seed(db_session)
    sheet = tearsheet.tearsheet(
        db_session, tenant_id, pid, "AAPL",
        feed_enabled=True,
        technicals_reader=lambda: _TECH_ART_WITH_RATING,
        rating_reader=lambda: art,
    )
    tech = sheet.technical
    assert tech.tech_rating_score == 0.6 and tech.tech_rating_label == "Buy"
    assert tech.tech_rating_basis == "intraday"
    assert tech.tech_rating_ma_score == 0.7 and tech.tech_rating_osc_score == 0.5
    assert tech.tech_rating_n_votes == 11


def test_tearsheet_technical_rating_falls_back_to_eod(db_session):
    tenant_id, pid = _seed(db_session)
    sheet = tearsheet.tearsheet(
        db_session, tenant_id, pid, "AAPL",
        feed_enabled=True,
        technicals_reader=lambda: _TECH_ART_WITH_RATING,
        rating_reader=lambda: None,  # producer artifact absent (sibling PR not merged yet)
    )
    tech = sheet.technical
    assert tech.tech_rating_score == -0.2 and tech.tech_rating_label == "Sell"
    assert tech.tech_rating_basis == "eod"


def test_tearsheet_technical_rating_absent_producer_omits_fields(db_session):
    """The sibling producer artifact doesn't exist yet — both readers empty → omitted,
    never fabricated (metron-ops#294 depends-on)."""
    tenant_id, pid = _seed(db_session)
    sheet = tearsheet.tearsheet(
        db_session, tenant_id, pid, "AAPL",
        feed_enabled=True,
        technicals_reader=lambda: _TECH_ART,  # v2-shaped, no embedded rating
        rating_reader=lambda: None,
    )
    tech = sheet.technical
    assert tech.tech_rating_score is None and tech.tech_rating_label is None


_FUND_ART = {
    "as_of": "2026-06-17",
    "fundamentals": {
        "AAPL": {
            "sector": "Technology", "industry": "Consumer Electronics", "marketCap": 3.2e12, "beta": 1.2,
            "trailingPE": 30.0, "forwardPE": 28.0, "enterpriseToEbitda": 22.0,
            "earningsGrowth": 0.1, "revenueGrowth": 0.08, "debtToEquity": 150.0,
            "currentRatio": 1.1, "quickRatio": 0.9, "returnOnEquity": 0.5, "returnOnAssets": 0.2,
            "grossMargins": 0.44, "operatingMargins": 0.30, "dividendYield": 0.5,
        }
    },
}


def test_tearsheet_fundamentals_populate_when_feed_enabled(db_session):
    tenant_id, pid = _seed(db_session)
    sheet = tearsheet.tearsheet(
        db_session, tenant_id, pid, "AAPL", feed_enabled=True,
        fundamentals_reader=lambda: _FUND_ART,
        performance_reader=lambda: _PERF_ART,
        technicals_reader=lambda: _TECH_ART,
    )
    assert sheet.fundamentals_available is True
    assert sheet.fundamentals is not None
    assert sheet.fundamentals.trailing_pe == 30.0
    assert sheet.fundamentals.peg == pytest.approx(3.0)
    assert sheet.fundamentals.dividend_yield == pytest.approx(0.005)
    assert sheet.technical.forward_div_yield == pytest.approx(0.005)
    assert any(c.is_self and c.ticker == "AAPL" for c in sheet.comps)


def test_tearsheet_fundamentals_omitted_when_feed_off(db_session):
    tenant_id, pid = _seed(db_session)
    sheet = tearsheet.tearsheet(
        db_session, tenant_id, pid, "AAPL", feed_enabled=False, fundamentals_reader=lambda: _FUND_ART
    )
    assert sheet.fundamentals_available is False
    assert sheet.fundamentals is None


def test_tearsheet_none_when_not_held(db_session):
    tenant_id, pid = _seed(db_session)
    assert tearsheet.tearsheet(db_session, tenant_id, pid, "TSLA") is None


def test_tearsheet_endpoint_404_for_unheld(client):
    tenant = str(uuid.uuid4())
    pid = client.post("/portfolios", json={"name": "P"}, headers={"X-Tenant-Id": tenant}).json()["id"]
    r = client.get(f"/portfolios/{pid}/tearsheet/TSLA", headers={"X-Tenant-Id": tenant})
    assert r.status_code == 404


def _seed_crwd(session):
    tenant = models.Tenant(name="t")
    session.add(tenant)
    session.flush()
    pf = models.Portfolio(tenant_id=tenant.id, name="P", base_currency="USD")
    session.add(pf)
    session.flush()
    acct = models.Account(tenant_id=tenant.id, portfolio_id=pf.id, broker="csv", external_id="CSV-1", currency="USD")
    crwd = models.Security(symbol="CRWD", currency="USD")
    session.add_all([acct, crwd])
    session.flush()
    session.add(
        models.Transaction(
            tenant_id=tenant.id, account_id=acct.id, security_id=crwd.id, txn_type="BUY",
            quantity=10, price=100.0, amount=1000.0, currency="USD",
            trade_date=date(2025, 1, 1), source_key="buy-crwd",
        )
    )
    session.commit()
    return tenant.id, pf.id


def test_tearsheet_period_return_from_spine_artifact(db_session):
    """Tearsheet 1Y comes from the security_performance spine — not local price_bars."""
    tenant_id, pid = _seed_crwd(db_session)
    ltm = 193.98 / 126.36 - 1.0
    perf_art = {
        "as_of": "2026-07-02",
        "performance": {
            "CRWD": {
                "period_returns": {"1Y": ltm},
                "ltm_pct": ltm,
                "n_bars": 252,
                "history_from": "2025-07-06",
            },
        },
    }
    sheet = tearsheet.tearsheet(
        db_session, tenant_id, pid, "CRWD",
        feed_enabled=True,
        performance_reader=lambda: perf_art,
    )
    assert sheet is not None
    assert sheet.performance.period_returns["1Y"] == pytest.approx(ltm, rel=1e-3)


def test_security_performance_spine_consumer_parses_artifact():
    """Contract: tearsheet consumer reads the security_performance spine shape."""
    art = {
        "as_of": "2026-07-02",
        "performance": {
            "AAPL": {
                "period_returns": {"1Y": 0.15, "3Y": 0.40},
                "ytd_pct": 0.10,
                "ltm_pct": 0.12,
                "volatility": 0.25,
                "sharpe": 1.2,
                "sortino": 1.5,
                "max_drawdown": -0.08,
                "beta_vs_spy": 1.1,
                "vs_spy_1y": 0.03,
                "vs_spy_window": 0.02,
                "n_bars": 500,
                "history_from": "2024-01-02",
            },
        },
    }
    snap = security_performance.load_security_performance(reader=lambda: art)
    row = snap.by_symbol["AAPL"]
    assert snap.as_of == date(2026, 7, 2)
    assert row.period_returns["1Y"] == pytest.approx(0.15)
    assert row.vs_spy_1y == pytest.approx(0.03)
    assert row.history_from == date(2024, 1, 2)
