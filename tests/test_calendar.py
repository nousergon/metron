"""Upcoming-events calendar — held-ticker earnings (C2-6d).

Injected earnings source (never the network): refresh caches each held ticker's next
earnings date, and the calendar surfaces only the held tickers with a date inside the
horizon — sorted, deduped, never fabricated.
"""

from __future__ import annotations

import io
import uuid
from datetime import UTC, date, datetime, timedelta

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
        cal = calendar.upcoming_events(
            db_session, uuid.UUID(tenant), uuid.UUID(pid), today=TODAY, macro_events_source=lambda: []
        )
        assert cal.n_events == 0 and cal.events == []
        assert cal.earnings_sourced_at is None  # never refreshed — no fabricated stamp

    def test_refresh_stamps_sourced_at(self, client, db_session, tenant):
        # metron-ops#149 item 2: a refresh stamps next_earnings_sourced_at, and the
        # calendar summary surfaces the newest stamp across this portfolio's held tickers.
        pid = _seed(client, tenant)
        now = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)
        n = calendar.refresh_earnings(db_session, ["AAPL", "MSFT"], source=_earnings_src, now=now)
        assert n == 2
        cal = calendar.upcoming_events(
            db_session, uuid.UUID(tenant), uuid.UUID(pid), today=TODAY, macro_events_source=lambda: []
        )
        # SQLite stores/returns naive datetimes (strips tzinfo) — compare naively, same as
        # every other `datetime` column round-tripped through the personal-tier DB.
        assert cal.earnings_sourced_at == now.replace(tzinfo=None)

    def test_refresh_stamp_advances_on_a_later_refresh(self, client, db_session, tenant):
        pid = _seed(client, tenant)
        first = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)
        second = datetime(2024, 6, 2, 9, 0, tzinfo=UTC)
        calendar.refresh_earnings(db_session, ["AAPL"], source=_earnings_src, now=first)
        calendar.refresh_earnings(db_session, ["AAPL"], source=_earnings_src, now=second)
        cal = calendar.upcoming_events(
            db_session, uuid.UUID(tenant), uuid.UUID(pid), today=TODAY, macro_events_source=lambda: []
        )
        assert cal.earnings_sourced_at == second.replace(tzinfo=None)

    def test_macro_events_merged_and_sorted(self, client, db_session, tenant):
        # Macro events (FOMC + releases) merge with held-ticker earnings, filtered to the
        # horizon and sorted by date (metron-ops#49).
        pid = _seed(client, tenant)
        calendar.refresh_earnings(db_session, ["AAPL", "MSFT"], source=_earnings_src)

        def macro_src():
            return [
                {"date": "2024-06-05", "kind": "release", "series_id": "UNRATE", "label": "Employment Situation"},
                {"date": "2024-06-18", "kind": "fomc", "series_id": "FOMC", "label": "FOMC Meeting"},
                {"date": "2030-01-01", "kind": "fomc", "series_id": "FOMC", "label": "Beyond horizon"},  # filtered
            ]

        cal = calendar.upcoming_events(
            db_session, uuid.UUID(tenant), uuid.UUID(pid), today=TODAY, macro_events_source=macro_src
        )
        # UNRATE 6/05, AAPL earnings 6/11, FOMC 6/18 — sorted by date; the 2030 event dropped.
        assert [(e.event_date, e.kind) for e in cal.events] == [
            (date(2024, 6, 5), "release"),
            (date(2024, 6, 11), "earnings"),
            (date(2024, 6, 18), "fomc"),
        ]

    def test_macro_events_show_without_holdings(self, client, db_session, tenant):
        # An empty portfolio still shows global macro events (they aren't portfolio-scoped).
        pid = client.post("/portfolios", json={"name": "Empty"}, headers={"X-Tenant-Id": tenant}).json()["id"]

        def macro_src():
            return [{"date": "2024-06-18", "kind": "fomc", "series_id": "FOMC", "label": "FOMC Meeting"}]

        cal = calendar.upcoming_events(
            db_session, uuid.UUID(tenant), uuid.UUID(pid), today=TODAY, macro_events_source=macro_src
        )
        assert cal.n_events == 1 and cal.events[0].kind == "fomc"


class TestCalendarEndpoints:
    def test_refresh_then_get(self, client, tenant, monkeypatch):
        pid = _seed(client, tenant)
        # The endpoint values "today" as the real date, so source a future-relative date.
        def _future_src(syms, *, source=None):
            return {"AAPL": date.today() + timedelta(days=10)} if "AAPL" in syms else {}

        monkeypatch.setattr("api.services.calendar.fetch_earnings_dates", _future_src)
        assert client.get(f"/portfolios/{pid}/calendar", headers={"X-Tenant-Id": tenant}).json()["earnings_sourced_at"] is None
        posted = client.post(f"/portfolios/{pid}/calendar/refresh", headers={"X-Tenant-Id": tenant}).json()
        assert posted["n_events"] == 1 and posted["events"][0]["ticker"] == "AAPL"
        assert posted["earnings_sourced_at"] is not None
        got = client.get(f"/portfolios/{pid}/calendar", headers={"X-Tenant-Id": tenant}).json()
        assert got["n_events"] == 1
        assert got["earnings_sourced_at"] == posted["earnings_sourced_at"]

    def test_calendar_requires_ownership(self, client, tenant):
        pid = _seed(client, tenant)
        assert client.get(
            f"/portfolios/{pid}/calendar", headers={"X-Tenant-Id": str(uuid.uuid4())}
        ).status_code == 404
