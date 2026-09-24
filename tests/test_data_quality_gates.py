"""Layer-5 ingestion data-quality gates (metron-ops#219) wired into the real
chokepoints: ``persistence.persist_snapshot`` (schema contract),
``prices.refresh_latest_prices`` / ``prices.backfill_prices`` (price outlier and
adjusted-close continuity), the nightly ``daily_refresh`` (stale prices), and the
``GET /meta/status`` surface.

The load-bearing property in every test here: FLAG MODE. A finding is logged and
reported; the data that lands is byte-for-byte what would have landed without the
gate."""

from __future__ import annotations

import io
import logging
import uuid
from datetime import date

import pytest
from sqlalchemy import select

from api.db import models
from api.maintenance import daily_refresh
from api.services import data_quality, persistence
from api.services import prices as price_service
from portfolio_analytics.domain.ledger import TxnType
from portfolio_analytics.ingestion import quality
from portfolio_analytics.ingestion.base import ConnectorSnapshot
from portfolio_analytics.ingestion.schema import (
    CanonicalAccount,
    CanonicalActivity,
    CanonicalHolding,
    CanonicalSecurity,
)
from portfolio_analytics.prices import ClosePoint
from tests.test_maintenance import _no_derived, _seed, _spy_src

TODAY = date(2024, 6, 3)  # a Monday


@pytest.fixture(autouse=True)
def _fresh_report_memo():
    """The once-per-process log memo is global; isolate it per test."""
    data_quality._reported.clear()
    yield
    data_quality._reported.clear()


def _dq_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "[data-quality" in r.getMessage()]


def _tenant_portfolio(session):
    tenant = models.Tenant(name="t")
    session.add(tenant)
    session.flush()
    portfolio = models.Portfolio(tenant_id=tenant.id, name="p")
    session.add(portfolio)
    session.flush()
    return tenant, portfolio


def _security(session, symbol="AAPL", currency="USD"):
    sec = models.Security(symbol=symbol, currency=currency)
    session.add(sec)
    session.flush()
    return sec


def _bar(session, sec, d, close):
    session.add(models.PriceBar(security_id=sec.id, bar_date=d, close=close, currency=sec.currency))
    session.flush()


def _closes(session, sec):
    return {
        b.bar_date: float(b.close)
        for b in session.scalars(select(models.PriceBar).where(models.PriceBar.security_id == sec.id)).all()
    }


def _record_split(session, sec, when, ratio):
    tenant, portfolio = _tenant_portfolio(session)
    acct = models.Account(tenant_id=tenant.id, portfolio_id=portfolio.id, broker="csv", external_id="A")
    session.add(acct)
    session.flush()
    session.add(models.Transaction(tenant_id=tenant.id, account_id=acct.id, security_id=sec.id,
                                   txn_type="SPLIT", quantity=ratio, trade_date=when, source_key=f"split-{when}"))
    session.flush()


# ── price outlier / split continuity at refresh_latest_prices ──────────────────
class TestLatestCloseGate:
    def test_outlier_is_flagged_and_written_unchanged(self, db_session, caplog):
        sec = _security(db_session)
        _bar(db_session, sec, date(2024, 5, 31), 100.0)
        src = lambda symbols: {"AAPL": ClosePoint(TODAY, 40.0)}  # noqa: E731 — a −60% "print"

        with caplog.at_level(logging.WARNING, logger="api.services.data_quality"):
            written = price_service.refresh_latest_prices(db_session, ["AAPL"], source=src)

        assert written == 1
        assert _closes(db_session, sec) == {date(2024, 5, 31): 100.0, TODAY: 40.0}  # flag mode: stored as fetched
        lines = _dq_lines(caplog)
        assert len(lines) == 1 and lines[0].startswith("[data-quality:price_outlier] AAPL:")
        assert "(at refresh_latest_prices)" in lines[0]
        assert all(r.levelno == logging.WARNING for r in caplog.records)  # never ERROR → never pages

    def test_recorded_split_reports_continuity_not_outlier(self, db_session, caplog):
        sec = _security(db_session)
        _bar(db_session, sec, date(2024, 5, 31), 100.0)
        _record_split(db_session, sec, TODAY, 2.0)
        with caplog.at_level(logging.WARNING, logger="api.services.data_quality"):
            price_service.refresh_latest_prices(db_session, ["AAPL"], source=lambda s: {"AAPL": ClosePoint(TODAY, 50.5)})
        lines = _dq_lines(caplog)
        assert len(lines) == 1 and lines[0].startswith("[data-quality:split_discontinuity]")
        assert _closes(db_session, sec)[TODAY] == 50.5

    def test_first_ever_bar_and_normal_move_are_quiet(self, db_session, caplog):
        a, m = _security(db_session, "AAPL"), _security(db_session, "MSFT")
        _bar(db_session, m, date(2024, 5, 31), 100.0)
        src = lambda s: {"AAPL": ClosePoint(TODAY, 40.0), "MSFT": ClosePoint(TODAY, 101.0)}  # noqa: E731
        with caplog.at_level(logging.WARNING, logger="api.services.data_quality"):
            assert price_service.refresh_latest_prices(db_session, ["AAPL", "MSFT"], source=src) == 2
        assert _dq_lines(caplog) == []
        assert _closes(db_session, a) == {TODAY: 40.0}

    def test_same_day_rewrite_compares_against_the_bar_before_it(self, db_session):
        sec = _security(db_session)
        _bar(db_session, sec, date(2024, 5, 31), 100.0)
        _bar(db_session, sec, TODAY, 99.0)
        found = data_quality.gate_latest_closes(db_session, [(sec, ClosePoint(TODAY, 45.0))], today=TODAY)
        assert [f.gate for f in found] == [quality.GATE_PRICE_OUTLIER]
        assert found[0].observed == pytest.approx(-0.55)

    def test_a_broken_gate_never_blocks_the_write(self, db_session, caplog, monkeypatch):
        sec = _security(db_session)

        def boom(*a, **k):
            raise RuntimeError("gate bug")

        monkeypatch.setattr(quality, "check_price_series", boom)
        with caplog.at_level(logging.WARNING, logger="api.services.data_quality"):
            assert price_service.refresh_latest_prices(db_session, ["AAPL"], source=lambda s: {"AAPL": ClosePoint(TODAY, 1.0)}) == 1
        assert _closes(db_session, sec) == {TODAY: 1.0}
        assert any("gate at refresh_latest_prices raised" in r.getMessage() for r in caplog.records)
        assert all(r.levelno < logging.ERROR for r in caplog.records)


# ── price outlier at backfill_prices ─────────────────────────────────────────────
class TestCloseHistoryGate:
    def _history(self, points):
        return lambda symbols, start, end: {"AAPL": points} if "AAPL" in symbols else {}

    def test_spike_inside_the_series_is_flagged_and_every_bar_lands(self, db_session, caplog):
        sec = _security(db_session)
        pts = [ClosePoint(date(2024, 5, 28), 100.0), ClosePoint(date(2024, 5, 29), 180.0),
               ClosePoint(date(2024, 5, 30), 101.0)]
        with caplog.at_level(logging.WARNING, logger="api.services.data_quality"):
            price_service.backfill_prices(db_session, ["AAPL"], date(2024, 5, 28), date(2024, 5, 30),
                                          source=self._history(pts))
        assert _closes(db_session, sec) == {p.bar_date: p.close for p in pts}
        gates = [line.split("]")[0] for line in _dq_lines(caplog)]
        assert gates == ["[data-quality:price_outlier", "[data-quality:price_outlier"]  # up, then back down

    def test_seam_with_cached_history_before_start(self, db_session):
        sec = _security(db_session)
        _bar(db_session, sec, date(2024, 5, 1), 100.0)
        _bar(db_session, sec, date(2024, 5, 24), 200.0)  # the latest bar before start is the seam
        found = data_quality.gate_close_history(
            db_session, {sec.id: ("AAPL", [ClosePoint(date(2024, 5, 28), 100.0)])}, start=date(2024, 5, 28),
            today=TODAY,
        )
        assert [(f.gate, f.as_of) for f in found] == [(quality.GATE_PRICE_OUTLIER, date(2024, 5, 28))]

    def test_empty_input(self, db_session):
        assert data_quality.gate_close_history(db_session, {}, start=TODAY) == []

    def test_one_log_line_per_finding_per_process(self, db_session, caplog):
        """backfill runs 3x per portfolio per night over the whole history; an old
        outlier must not print a dozen identical lines."""
        _security(db_session)
        pts = [ClosePoint(date(2024, 5, 28), 100.0), ClosePoint(date(2024, 5, 29), 10.0)]
        with caplog.at_level(logging.WARNING, logger="api.services.data_quality"):
            for _ in range(3):
                price_service.backfill_prices(db_session, ["AAPL"], date(2024, 5, 28), date(2024, 5, 29),
                                              source=self._history(pts))
        assert len(_dq_lines(caplog)) == 1

    def test_memo_is_bounded(self, monkeypatch):
        monkeypatch.setattr(data_quality, "_REPORTED_CAP", 2)
        fs = [quality.Finding("g", f"s{i}", "d") for i in range(3)]
        data_quality._report(fs, where="t")
        assert len(data_quality._reported) == 1  # cleared on overflow, then the third recorded


# ── schema contract at persist_snapshot ─────────────────────────────────────────
def _snapshot(activities, *, holdings=(), source="ibkr_flex"):
    sec = CanonicalSecurity(security_id="EQ:AAPL:USD", ticker="AAPL")
    return ConnectorSnapshot(
        source=source,
        accounts=[CanonicalAccount(number="U1"), CanonicalAccount(number="U2")],
        securities=[sec],
        holdings=list(holdings),
        activities=list(activities),
    )


class TestSnapshotContractGate:
    def test_violating_rows_are_reported_and_still_persisted(self, db_session, caplog):
        tenant, portfolio = _tenant_portfolio(db_session)
        bad = CanonicalActivity(account_number="U1", when=date(2024, 1, 2), type=TxnType.BUY,
                                security_id="EQ:AAPL:USD", quantity=0, price=10)
        neg = CanonicalActivity(account_number="U1", when=date(2024, 1, 3), type=TxnType.DIVIDEND,
                                security_id="EQ:AAPL:USD", amount=-4.0)
        with caplog.at_level(logging.WARNING, logger="api.services.data_quality"):
            result = persistence.persist_snapshot(db_session, tenant_id=tenant.id, portfolio_id=portfolio.id,
                                                  snapshot=_snapshot([bad, neg]))
        assert len(result.data_quality_findings) == 2
        assert {f.gate for f in result.data_quality_findings} == {quality.GATE_CONTRACT}
        # FLAG MODE: both rows landed exactly as the connector produced them.
        assert result.transactions_inserted == 2
        rows = db_session.scalars(select(models.Transaction).order_by(models.Transaction.trade_date)).all()
        assert [(r.txn_type, float(r.quantity), float(r.amount)) for r in rows] == [("BUY", 0.0, 0.0),
                                                                                  ("DIVIDEND", 0.0, -4.0)]
        assert len(_dq_lines(caplog)) == 2
        assert "(at persist_snapshot[ibkr_flex])" in _dq_lines(caplog)[0]

    def test_clean_snapshot_reports_nothing(self, db_session):
        tenant, portfolio = _tenant_portfolio(db_session)
        ok = CanonicalActivity(account_number="U1", when=date(2024, 1, 2), type=TxnType.BUY,
                               security_id="EQ:AAPL:USD", quantity=1, price=10)
        result = persistence.persist_snapshot(db_session, tenant_id=tenant.id, portfolio_id=portfolio.id,
                                              snapshot=_snapshot([ok]))
        assert result.data_quality_findings == []

    def test_user_deleted_account_is_not_a_dangling_reference(self, db_session):
        """The contract runs on the snapshot as the connector produced it — before the
        exclusion filter — so a deleted account's rows aren't misread as orphans."""
        tenant, portfolio = _tenant_portfolio(db_session)
        db_session.add(models.InvestorPreferences(tenant_id=tenant.id, portfolio_id=portfolio.id,
                                                  excluded_account_keys="ibkr_flex:U2"))
        db_session.flush()
        held = CanonicalHolding(account_number="U2", security_id="EQ:AAPL:USD", quantity=1)
        result = persistence.persist_snapshot(db_session, tenant_id=tenant.id, portfolio_id=portfolio.id,
                                              snapshot=_snapshot([], holdings=[held]))
        assert result.accounts_excluded == 1
        assert result.data_quality_findings == []

    def test_import_response_carries_the_findings(self, client):
        tenant = str(uuid.uuid4())
        hdr = {"X-Tenant-Id": tenant}
        pid = client.post("/portfolios", json={"name": "P"}, headers=hdr).json()["id"]
        csv = "date,type,symbol,quantity,price\n2024-01-02,BUY,AAPL,10,100\n2024-01-03,BUY,MSFT,0,100\n"
        r = client.post(f"/portfolios/{pid}/import/csv",
                        files={"file": ("t.csv", io.BytesIO(csv.encode()), "text/csv")}, headers=hdr)
        assert r.status_code == 200
        body = r.json()
        assert body["transactions_inserted"] == 2  # the zero-quantity row still landed
        assert len(body["data_quality_findings"]) == 1
        assert body["data_quality_findings"][0].startswith("[data-quality:schema_contract] csv:activity[1]:")

    def test_clean_import_has_empty_findings(self, client):
        hdr = {"X-Tenant-Id": str(uuid.uuid4())}
        pid = client.post("/portfolios", json={"name": "P"}, headers=hdr).json()["id"]
        csv = "date,type,symbol,quantity,price\n2024-01-02,BUY,AAPL,10,100\n"
        r = client.post(f"/portfolios/{pid}/import/csv",
                        files={"file": ("t.csv", io.BytesIO(csv.encode()), "text/csv")}, headers=hdr)
        assert r.json()["data_quality_findings"] == []


# ── stale prices ────────────────────────────────────────────────────────────────
class TestStalePriceGate:
    def test_threshold_is_the_holdings_badge_threshold(self):
        from api.services.security_perf import STALE_AFTER_SESSIONS

        assert data_quality.STALE_PRICE_AFTER_SESSIONS == STALE_AFTER_SESSIONS

    def test_stale_findings_use_the_nyse_calendar(self):
        found = data_quality.stale_findings(
            {"FRESH": date(2024, 5, 31), "OLD": date(2024, 5, 29), "ANCIENT": date(2010, 1, 4)}, today=TODAY
        )
        # Fri 5/31 → Mon 6/3 is 1 session (fresh); Wed 5/29 is 3 behind; 2010 predates the calendar.
        by = {f.subject: f for f in found}
        assert set(by) == {"OLD", "ANCIENT"}
        assert by["OLD"].observed == 3.0 and by["OLD"].gate == quality.GATE_STALE_PRICE
        assert by["ANCIENT"].observed is None and "older than the NYSE calendar" in by["ANCIENT"].detail

    def test_uncalendared_today_is_not_guessed(self):
        assert data_quality.stale_findings({"X": date(2024, 5, 1)}, today=date(2040, 1, 2)) == []

    def test_gate_reads_cached_closes_and_skips_never_priced(self, db_session, caplog):
        a, _ = _security(db_session, "AAPL"), _security(db_session, "CIT")
        _bar(db_session, a, date(2024, 5, 20), 100.0)
        with caplog.at_level(logging.WARNING, logger="api.services.data_quality"):
            found = data_quality.gate_stale_prices(db_session, ["AAPL", "CIT"], today=TODAY)
        assert [f.subject for f in found] == ["AAPL"]
        assert _dq_lines(caplog)[0].startswith("[data-quality:stale_price] AAPL:")
        assert _closes(db_session, a) == {date(2024, 5, 20): 100.0}

    def test_daily_refresh_counts_stale_flags(self, client, db_session, monkeypatch):
        # The source only has a two-week-old close for AAPL.
        old = ClosePoint(bar_date=date(2024, 5, 20), close=150.0)
        monkeypatch.setattr("api.services.prices.fetch_latest_closes", lambda s, **k: {"AAPL": old})
        monkeypatch.setattr("api.services.performance.fetch_latest_closes", _spy_src)
        monkeypatch.setattr("api.maintenance.fetch_latest_closes", _spy_src)
        _no_derived(monkeypatch)
        _seed(client, str(uuid.uuid4()))
        result = daily_refresh(db_session, today=TODAY)
        assert result.stale_prices_flagged == 1
        assert result.prices_updated == 1  # the stale close was still cached — flag, don't block


# ── /meta/status ────────────────────────────────────────────────────────────────
class TestStatusSurface:
    def _held(self, session, symbol):
        tenant, portfolio = _tenant_portfolio(session)
        acct = models.Account(tenant_id=tenant.id, portfolio_id=portfolio.id, broker="ibkr_flex", external_id=symbol)
        session.add(acct)
        session.flush()
        sec = _security(session, symbol)
        session.add(models.Position(tenant_id=tenant.id, account_id=acct.id, security_id=sec.id,
                                    quantity=1, avg_cost=1, as_of=TODAY))
        session.flush()
        return sec

    def test_summary_counts(self, db_session):
        stale = self._held(db_session, "OLD")
        _bar(db_session, stale, date(2024, 5, 20), 10.0)
        spiky = self._held(db_session, "SPIKE")
        _bar(db_session, spiky, date(2024, 5, 30), 10.0)
        _bar(db_session, spiky, date(2024, 5, 31), 30.0)
        split = _security(db_session, "SPLT")  # not broker-held: still scanned for moves
        _bar(db_session, split, date(2024, 5, 30), 100.0)
        _bar(db_session, split, date(2024, 5, 31), 50.0)
        _record_split(db_session, split, date(2024, 5, 31), 2.0)
        _bar(db_session, split, date(2024, 1, 2), 1.0)  # outside the window: never read
        db_session.commit()

        s = data_quality.status_summary(db_session, today=TODAY)
        assert s["available"] is True and s["mode"] == "flag"
        assert s["held_securities_priced"] == 2
        assert s["stale_prices"] == 1
        assert s["price_outliers"] == 1
        assert s["split_discontinuities"] == 1
        assert s["invalid_prices"] == 0
        assert s["thresholds"]["price_outlier_max_move"] == quality.PRICE_OUTLIER_MAX_MOVE
        # Counts only: /status is system-wide, so no symbol may leak into it.
        assert "OLD" not in repr(s) and "SPIKE" not in repr(s)

    def test_status_endpoint_carries_the_block(self, client):
        body = client.get("/meta/status").json()
        dq = body["data_quality"]
        assert dq["available"] is True
        assert dq["stale_prices"] == 0 and dq["price_outliers"] == 0

    def test_status_survives_a_failing_summary(self, client, monkeypatch, caplog):
        def boom(*a, **k):
            raise RuntimeError("db hiccup")

        monkeypatch.setattr(data_quality, "status_summary", boom)
        with caplog.at_level(logging.WARNING):
            r = client.get("/meta/status")
        assert r.status_code == 200
        assert r.json()["data_quality"] == {"available": False, "mode": "flag", "error": "RuntimeError"}
