"""Performance with contribution flows (metron-ops#44).

A portfolio funded by BUYS (no cash-deposit records) must NOT read its contribution-driven
build-up as investment return. The flow TWR neutralizes is NET PURCHASES (ΣBUY − ΣSELL),
not cash deposits. Reproduces the live bug: +6731% TWR / +4972% cumulative / 438% vol on a
2-year DCA portfolio — all of which were contributions, not gains.
"""

from __future__ import annotations

import io
import uuid
from datetime import date, timedelta

import pytest

from api.services import performance
from portfolio_analytics.prices import ClosePoint


def _flat(price: float):
    def _src(symbols, start, end):
        days = [start + timedelta(d) for d in range((end - start).days + 1)]
        return {s: [ClosePoint(bar_date=d, close=price) for d in days] for s in symbols}

    return _src


def _step(early: float, late: float, cutoff: date):
    """Flat at ``early`` (= the DCA transaction price, so a buy never lands at a discount),
    then steps to ``late`` after ``cutoff`` — a clean +X% appreciation on the whole book."""

    def _src(symbols, start, end):
        days = [start + timedelta(d) for d in range((end - start).days + 1)]
        return {
            s: [ClosePoint(bar_date=d, close=(late if d >= cutoff else early)) for d in days]
            for s in symbols
        }

    return _src


def _hdr(t: str) -> dict:
    return {"X-Tenant-Id": t}


def _seed_dca(client, tenant: str) -> str:
    # 12 monthly $1,200 buys of AAPL (12 sh @ $100). Pure DCA; no deposit records.
    rows = "date,type,symbol,quantity,price,amount,account\n"
    for m in range(1, 13):
        rows += f"2024-{m:02d}-05,BUY,AAPL,12,100,1200,Brokerage\n"
    pid = client.post("/portfolios", json={"name": "P"}, headers=_hdr(tenant)).json()["id"]
    r = client.post(
        f"/portfolios/{pid}/import/csv",
        files={"file": ("t.csv", io.BytesIO(rows.encode()), "text/csv")},
        headers=_hdr(tenant),
    )
    assert r.status_code == 200
    return pid


def test_dca_flat_prices_is_near_zero_return(client, db_session):
    tenant = str(uuid.uuid4())
    pid = _seed_dca(client, tenant)
    performance.reconstruct_snapshots(
        db_session, uuid.UUID(tenant), uuid.UUID(pid), today=date(2024, 12, 31), source=_flat(100.0)
    )
    p = client.get(f"/portfolios/{pid}/performance", headers=_hdr(tenant)).json()
    assert p["n_snapshots"] >= 2
    # Prices flat, growth is 100% contributions → ~0% return (NOT thousands of %).
    assert abs(p["twr"]) < 0.02
    assert abs(p["cumulative_return"]) < 0.02
    # NAV is the value built up by contributions (~$14,400) — that's value, not return.
    assert p["latest_nav"] == pytest.approx(14400, rel=0.02)
    # Annualized + volatility no longer explode from contribution jumps.
    if p["annualized_twr"] is not None:
        assert abs(p["annualized_twr"]) < 0.10
    if p["volatility"] is not None:
        assert p["volatility"] < 0.20


def test_dca_with_real_gain_reads_the_gain_not_the_contributions(client, db_session):
    tenant = str(uuid.uuid4())
    pid = _seed_dca(client, tenant)
    # Buys all at $100 (= market), then the whole book appreciates +10% near year-end.
    performance.reconstruct_snapshots(
        db_session, uuid.UUID(tenant), uuid.UUID(pid),
        today=date(2024, 12, 31), source=_step(100.0, 110.0, date(2024, 12, 20)),
    )
    p = client.get(f"/portfolios/{pid}/performance", headers=_hdr(tenant)).json()
    # ~+10% (the appreciation), NOT hundreds/thousands of % from the contributions.
    assert p["twr"] == pytest.approx(0.10, abs=0.02)
    assert p["cumulative_return"] == pytest.approx(0.10, abs=0.02)
    # NAV ≈ 144 shares × $110.
    assert p["latest_nav"] == pytest.approx(144 * 110, rel=0.02)
