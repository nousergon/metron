"""A revised close or FX rate never erases the value it replaces (metron-ops-I370).

``price_bars`` and ``fx_rates`` are rewritten in place: the nightly backfill overwrites a
cached close the source has since revised, and the latest-rate refresh overwrites a same-day
FX rate. ``nav_snapshot_revisions`` preserves a valuation a dated NAV point served, but the
input that valuation was computed from was gone the moment the source revised it, so the
old value could not be replayed from what Metron stored. Every superseded close and rate is
now appended to ``market_input_revisions`` in the same transaction that replaces it.

Synthetic prices and rates only.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select

from api.db import models
from api.services import fx as fx_service
from api.services import prices as price_service
from portfolio_analytics.prices import ClosePoint

D1, D2 = date(2024, 5, 28), date(2024, 5, 29)


def _security(session, symbol="AAPL"):
    sec = models.Security(symbol=symbol, currency="USD")
    session.add(sec)
    session.commit()
    return sec


def _history(points):
    return lambda symbols, start, end: {"AAPL": points} if "AAPL" in symbols else {}


def _latest(rates):
    def src(symbols, *, source=None):
        return {s: rates[s] for s in symbols if s in rates}

    return src


def _revisions(session):
    return session.scalars(
        select(models.MarketInputRevision).order_by(models.MarketInputRevision.superseded_at)
    ).all()


class TestBackfillRevision:
    def test_revised_close_preserves_the_superseded_close(self, db_session):
        sec = _security(db_session)
        price_service.backfill_prices(
            db_session, ["AAPL"], D1, D2, source=_history([ClosePoint(D1, 100.0), ClosePoint(D2, 101.0)])
        )
        # The source revises D1 only; D2 is unchanged.
        price_service.backfill_prices(
            db_session, ["AAPL"], D1, D2, source=_history([ClosePoint(D1, 95.5), ClosePoint(D2, 101.0)])
        )
        revs = _revisions(db_session)
        assert [(r.kind, r.security_id, r.obs_date, float(r.value)) for r in revs] == [
            ("price", sec.id, D1, 100.0)
        ]
        live = db_session.scalars(select(models.PriceBar).where(models.PriceBar.bar_date == D1)).one()
        assert float(live.close) == 95.5

    def test_identical_rerun_records_nothing(self, db_session):
        """The backfill re-sends the whole history three times a night: an unchanged bar,
        including a close with more digits than the column stores, is not a revision."""
        _security(db_session)
        pts = [ClosePoint(D1, 187.30999755859375), ClosePoint(D2, 101.0)]
        for _ in range(3):
            price_service.backfill_prices(db_session, ["AAPL"], D1, D2, source=_history(pts))
        assert _revisions(db_session) == []


class TestLatestRefreshRevision:
    def test_refreshed_close_preserves_the_superseded_close(self, db_session):
        sec = _security(db_session)
        price_service.refresh_latest_prices(db_session, ["AAPL"], source=_latest({"AAPL": ClosePoint(D1, 100.0)}))
        price_service.refresh_latest_prices(db_session, ["AAPL"], source=_latest({"AAPL": ClosePoint(D1, 100.0)}))
        assert _revisions(db_session) == []
        price_service.refresh_latest_prices(db_session, ["AAPL"], source=_latest({"AAPL": ClosePoint(D1, 102.0)}))
        revs = _revisions(db_session)
        assert [(r.kind, r.security_id, r.obs_date, float(r.value)) for r in revs] == [
            ("price", sec.id, D1, 100.0)
        ]

    def test_refreshed_fx_rate_preserves_the_superseded_rate(self, db_session):
        fx_service.refresh_fx_rates(db_session, ["HKD"], source=_latest({"HKDUSD=X": ClosePoint(D1, 0.128)}))
        fx_service.refresh_fx_rates(db_session, ["HKD"], source=_latest({"HKDUSD=X": ClosePoint(D1, 0.128)}))
        assert _revisions(db_session) == []
        fx_service.refresh_fx_rates(db_session, ["HKD"], source=_latest({"HKDUSD=X": ClosePoint(D1, 0.130)}))
        revs = _revisions(db_session)
        assert [(r.kind, r.currency, r.base, r.obs_date, float(r.value)) for r in revs] == [
            ("fx", "HKD", "USD", D1, 0.128)
        ]
        assert fx_service.rate_as_of(db_session, "HKD", D1) == 0.130


def test_deleted_close_is_preserved(db_session):
    sec = _security(db_session)
    price_service.refresh_latest_prices(db_session, ["AAPL"], source=_latest({"AAPL": ClosePoint(D1, 100.0)}))
    db_session.delete(db_session.scalars(select(models.PriceBar)).one())
    db_session.commit()
    revs = _revisions(db_session)
    assert [(r.kind, r.security_id, r.obs_date, float(r.value), r.change) for r in revs] == [
        ("price", sec.id, D1, 100.0, "delete")
    ]
