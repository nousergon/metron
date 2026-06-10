"""The daily-refresh maintenance job: prices + NAV snapshots across all portfolios."""

from __future__ import annotations

import io
import uuid
from datetime import date

from sqlalchemy import select

from api.db import models
from api.maintenance import daily_refresh, main
from portfolio_analytics.prices import ClosePoint

CSV = "date,type,symbol,quantity,price\n2024-01-01,BUY,AAPL,10,100\n"  # 10 sh, cost 1000
_AAPL = ClosePoint(bar_date=date(2024, 6, 3), close=150.0)  # MV 1500
_SPY = ClosePoint(bar_date=date(2024, 6, 3), close=500.0)


def _price_src(symbols, *, source=None):
    return {s: _AAPL for s in symbols if s == "AAPL"}


def _spy_src(symbols, *, source=None):
    return {"SPY": _SPY} if "SPY" in symbols else {}


def _hdr(tenant):
    return {"X-Tenant-Id": tenant}


def _seed(client, tenant, name="P"):
    pid = client.post("/portfolios", json={"name": name}, headers=_hdr(tenant)).json()["id"]
    r = client.post(
        f"/portfolios/{pid}/import/csv",
        files={"file": ("t.csv", io.BytesIO(CSV.encode()), "text/csv")},
        headers=_hdr(tenant),
    )
    assert r.status_code == 200
    return pid


def _navsnaps(pid):
    return select(models.NavSnapshot).where(models.NavSnapshot.portfolio_id == uuid.UUID(pid))


def test_daily_refresh_records_snapshots_for_all_portfolios(client, db_session, monkeypatch):
    monkeypatch.setattr("api.services.prices.fetch_latest_closes", _price_src)
    monkeypatch.setattr("api.services.performance.fetch_latest_closes", _spy_src)
    # Two tenants, one portfolio each — the operator job sweeps both.
    t1, t2 = str(uuid.uuid4()), str(uuid.uuid4())
    p1 = _seed(client, t1, "A")
    p2 = _seed(client, t2, "B")

    result = daily_refresh(db_session, today=date(2024, 6, 3))

    assert result.portfolios == 2
    assert result.snapshots_recorded == 2
    assert result.symbols == 2  # one AAPL holding each
    for pid in (p1, p2):
        snap = db_session.scalars(_navsnaps(pid)).first()
        assert float(snap.nav) == 1500.0
        assert float(snap.spy_close) == 500.0


def test_daily_refresh_idempotent_per_day(client, db_session, monkeypatch):
    monkeypatch.setattr("api.services.prices.fetch_latest_closes", _price_src)
    monkeypatch.setattr("api.services.performance.fetch_latest_closes", _spy_src)
    t = str(uuid.uuid4())
    pid = _seed(client, t)
    daily_refresh(db_session, today=date(2024, 6, 3))
    daily_refresh(db_session, today=date(2024, 6, 3))
    assert len(db_session.scalars(_navsnaps(pid)).all()) == 1  # same day → one row


def test_daily_refresh_skips_unpriceable_without_fabricating(client, db_session, monkeypatch):
    monkeypatch.setattr("api.services.prices.fetch_latest_closes", lambda s, *, source=None: {})
    monkeypatch.setattr("api.services.performance.fetch_latest_closes", lambda s, *, source=None: {})
    t = str(uuid.uuid4())
    _seed(client, t)
    result = daily_refresh(db_session, today=date(2024, 6, 3))
    assert result.portfolios == 1
    assert result.snapshots_recorded == 0  # nothing priceable → no fabricated NAV


def test_daily_refresh_empty_db_is_a_noop(db_session):
    result = daily_refresh(db_session, today=date(2024, 6, 3))
    assert result == type(result)(portfolios=0, symbols=0, prices_updated=0, snapshots_recorded=0)


def test_cli_unknown_command_errors():
    import pytest

    with pytest.raises(SystemExit):
        main(["bogus"])
