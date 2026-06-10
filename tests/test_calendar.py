"""Upcoming-events calendar — held-ticker earnings (C2-6d).

Injected earnings source (never the network): refresh caches each held ticker's next
earnings date, and the calendar surfaces only the held tickers with a date inside the
horizon — sorted, deduped, never fabricated.
"""

from __future__ import annotations

import io
import uuid
from datetime import date, timedelta

import pytest

from api.services import calendar

CSV = "date,type,symbol,quantity,price\n2024-01-01,BUY,AAPL,10,100\n2024-01-01,BUY,MSFT,5,200\n"
TODAY = date(2024, 6, 1)


def _earnings_src(symbols):
    out = {}
    if "AAPL" in symbols:
        out["AAPL"] = date(2024, 6, 11)  # in the 120-day horizon
    if "MSFT" in symbols:
        out["MSFT"] = date(2025, 7, 6)  # beyond the horizon → filtered
    return out


@pytest.fixture()
def tenant():
    return str(uuid.uuid4())


def _seed(client, tenant):
    pid = client.post("/portfolios", json={"name": "P"}, headers={"X-Tenant-Id": tenant}).json()["id"]
    assert client.post(
        f"/portfolios/{pid}/import/csv",
        files={"file": ("t.csv", io.BytesIO(CSV.encode()), "text/csv")},
        headers={"X-Tenant-Id": tenant},
    ).status_code == 200
    return pid


class TestCalendar:
    def test_refresh_then_upcoming_filters_horizon(self, client, db_session, tenant):
        pid = _seed(client, tenant)
        n = calendar.refresh_earnings(db_session, ["AAPL", "MSFT"], source=_earnings_src)
        assert n == 2
        cal = calendar.upcoming_events(db_session, uuid.UUID(tenant), uuid.UUID(pid), today=TODAY)
        assert cal.n_events == 1  # MSFT's date is beyond the 120-day horizon
        assert cal.events[0].ticker == "AAPL"
        assert cal.events[0].kind == "earnings"
        assert cal.events[0].event_date == date(2024, 6, 11)

    def test_empty_without_refresh(self, client, db_session, tenant):
        pid = _seed(client, tenant)
        cal = calendar.upcoming_events(db_session, uuid.UUID(tenant), uuid.UUID(pid), today=TODAY)
        assert cal.n_events == 0 and cal.events == []


class TestCalendarEndpoints:
    def test_refresh_then_get(self, client, tenant, monkeypatch):
        pid = _seed(client, tenant)
        # The endpoint values "today" as the real date, so source a future-relative date.
        def _future_src(syms, *, source=None):
            return {"AAPL": date.today() + timedelta(days=10)} if "AAPL" in syms else {}

        monkeypatch.setattr("api.services.calendar.fetch_earnings_dates", _future_src)
        posted = client.post(f"/portfolios/{pid}/calendar/refresh", headers={"X-Tenant-Id": tenant}).json()
        assert posted["n_events"] == 1 and posted["events"][0]["ticker"] == "AAPL"
        got = client.get(f"/portfolios/{pid}/calendar", headers={"X-Tenant-Id": tenant}).json()
        assert got["n_events"] == 1

    def test_calendar_requires_ownership(self, client, tenant):
        pid = _seed(client, tenant)
        assert client.get(
            f"/portfolios/{pid}/calendar", headers={"X-Tenant-Id": str(uuid.uuid4())}
        ).status_code == 404
