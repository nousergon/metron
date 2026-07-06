"""Consumer contract: tearsheet performance must match the security_performance spine."""

from __future__ import annotations

from datetime import date

import pytest

from api.services import security_performance


_ART = {
    "schema_version": 1,
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


def test_security_performance_consumer_parses_spine():
    snap = security_performance.load_security_performance(reader=lambda: _ART)
    row = snap.by_symbol["AAPL"]
    assert snap.as_of == date(2026, 7, 2)
    assert row.period_returns["1Y"] == pytest.approx(0.15)
    assert row.ytd_pct == pytest.approx(0.10)
    assert row.ltm_pct == pytest.approx(0.12)
    assert row.beta_vs_spy == pytest.approx(1.1)
    assert row.vs_spy_1y == pytest.approx(0.03)
    assert row.vs_spy_window == pytest.approx(0.02)
    assert row.history_from == date(2024, 1, 2)
