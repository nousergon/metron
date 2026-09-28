"""FX rate cache — fetch {CCY}USD=X, cache, look up; no fabrication on a missing rate."""

from __future__ import annotations

from datetime import date

import pytest

from api.db import models
from api.services import fx
from portfolio_analytics.prices import ClosePoint


def _fx_source(rates: dict[str, float]):
    def src(symbols, *, source=None):
        return {s: ClosePoint(date(2026, 6, 9), rates[s]) for s in symbols if s in rates}

    return src


class TestFx:
    def test_usd_is_identity(self, db_session):
        assert fx.latest_rate_to_base(db_session, "USD") == 1.0
        assert fx.latest_rate_to_base(db_session, "usd") == 1.0
        assert fx.latest_rate_to_base(db_session, "") == 1.0

    def test_missing_rate_is_none_not_one(self, db_session):
        # The no-fabrication rule: an unsourced currency returns None, never a 1.0 that
        # would silently treat 1 HKD as 1 USD.
        assert fx.latest_rate_to_base(db_session, "HKD") is None

    def test_refresh_and_lookup(self, db_session):
        n = fx.refresh_fx_rates(db_session, ["HKD", "GBP", "USD"], source=_fx_source({"HKDUSD=X": 0.128, "GBPUSD=X": 1.27}))
        assert n == 2  # USD skipped
        assert fx.latest_rate_to_base(db_session, "HKD") == pytest.approx(0.128)
        assert fx.latest_rate_to_base(db_session, "GBP") == pytest.approx(1.27)

    def test_refresh_is_idempotent_per_day(self, db_session):
        fx.refresh_fx_rates(db_session, ["HKD"], source=_fx_source({"HKDUSD=X": 0.128}))
        fx.refresh_fx_rates(db_session, ["HKD"], source=_fx_source({"HKDUSD=X": 0.130}))
        rows = db_session.query(models.FxRate).filter(models.FxRate.currency == "HKD").all()
        assert len(rows) == 1 and float(rows[0].rate) == pytest.approx(0.130)  # updated, not duplicated

    def test_rates_to_base_batch(self, db_session):
        fx.refresh_fx_rates(db_session, ["HKD"], source=_fx_source({"HKDUSD=X": 0.128}))
        out = fx.rates_to_base(db_session, ["USD", "HKD", "JPY"])
        assert out["USD"] == 1.0
        assert out["HKD"] == pytest.approx(0.128)
        assert out["JPY"] is None  # no rate cached


class TestRateHistory:
    """``rate_history`` answers exactly what ``rate_as_of`` / ``latest_rate_to_base`` answer,
    from one read — NAV reconstruction and the income/realized/transactions conversions
    use it instead of a query per (currency, date)."""

    def _seed(self, db_session):
        db_session.add_all([
            models.FxRate(currency="EUR", base="USD", rate_date=date(2026, 1, 5), rate=1.10),
            models.FxRate(currency="EUR", base="USD", rate_date=date(2026, 1, 9), rate=1.12),
            models.FxRate(currency="EUR", base="USD", rate_date=date(2026, 2, 2), rate=1.08),
            models.FxRate(currency="CHF", base="USD", rate_date=date(2026, 1, 7), rate=1.25),
            # Another base never answers a USD question.
            models.FxRate(currency="GBP", base="EUR", rate_date=date(2026, 1, 7), rate=1.15),
        ])
        db_session.commit()

    def test_matches_the_single_rate_lookups_on_every_date(self, db_session):
        self._seed(db_session)
        book = fx.rate_history(db_session, ["EUR", "chf ", "GBP", "USD", "HKD"])
        days = [date(2026, 1, 1) + (date(2026, 1, 2) - date(2026, 1, 1)) * i for i in range(45)]
        for ccy in ("EUR", "eur", "CHF", "GBP", "HKD", "USD", "", None):
            assert book.covers(ccy)
            assert book.latest(ccy) == fx.latest_rate_to_base(db_session, ccy)
            for d in days:
                assert book.rate_as_of(ccy, d) == fx.rate_as_of(db_session, ccy, d), (ccy, d)

    def test_batched_latest_rates_match_the_single_lookup(self, db_session):
        self._seed(db_session)
        wanted = ["EUR", "chf", "GBP", "HKD", "USD", ""]
        assert fx.rates_to_base(db_session, wanted) == {
            (c or "USD").strip().upper(): fx.latest_rate_to_base(db_session, c) for c in wanted
        }

    def test_before_the_first_rate_is_none_not_one(self, db_session):
        self._seed(db_session)
        book = fx.rate_history(db_session, ["EUR"])
        assert book.rate_as_of("EUR", date(2026, 1, 4)) is None
        assert book.rate_as_of("EUR", date(2026, 1, 5)) == pytest.approx(1.10)
        assert book.rate_as_of("EUR", date(2026, 1, 20)) == pytest.approx(1.12)  # carried forward

    def test_only_requested_currencies_are_covered(self, db_session):
        self._seed(db_session)
        book = fx.rate_history(db_session, ["EUR"])
        assert book.covers("EUR") and book.covers("USD")
        assert not book.covers("CHF")
