"""Runtime admission for the intraday-family spine readers (metron-ops-I373).

After metron#563 and #627 the remaining readers that never looked at ``schema_version`` and
converted numbers with a bare ``float()`` were the intraday ones: the Markets strip
(``indices``), the fund-proxy same-day move, the live holdings overlay (``intraday``), the
technical rating (intraday + EOD) and the rating track record. A ``schema_version: 999``
artifact was read as the known shape, and a ``"NaN"`` / ``"Infinity"`` last price became a
NaN index move or an infinite live position value. These cases go through the REAL readers
with synthetic artifacts.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime

import pytest

from api.services import indices, intraday, technical_rating, technical_rating_performance

NAN, INF = float("nan"), float("inf")
_NOW = datetime(2026, 9, 14, 15, 0, 0, tzinfo=UTC)  # a Monday, mid-session ET
_SESSION = "2026-09-14"


def _intraday(version=3, *, quote=None, proxy=None):
    quote = {"last": 101.0, "prev_close": 100.0, "open": 100.5, "session_date": _SESSION, **(quote or {})}
    proxy = {"last": 51.0, "prev_close": 50.0, "session_date": _SESSION, **(proxy or {})}
    return {
        "schema_version": version,
        "as_of_utc": "2026-09-14T14:58:00Z",
        "source": "yfinance_delayed",
        "quotes": {"AAPL": dict(quote)},
        "indices": {"SPY": dict(quote)},
        "fund_proxies": {"VOO": proxy},
    }


def _ratings(version=2, **row):
    return {"schema_version": version, "as_of_utc": "2026-09-14T14:58:00Z", "source": "computed_intraday",
            "ratings": {"AAPL": {"score": 0.4, "label": "Buy", "n_buy": 9, **row}}}


def _technicals(version=4, **row):
    return {"schema_version": version, "as_of": "2026-09-11", "source": "t",
            "technicals": {"MSFT": {"rating": {"score": -0.2, "label": "Neutral", **row}}}}


def _perf(version=1, **row):
    stats = {"ic_mean": 0.05, "buckets": {"Buy": {"n": 10, "mean_fwd": 0.01, **row}}}
    return {"schema_version": version, "as_of_utc": "2026-09-14T00:00:00Z",
            "segments": {"all": {"20": {"5": stats}}}}


@pytest.fixture(autouse=True)
def _no_pin(monkeypatch):
    monkeypatch.setattr(technical_rating.entitlements, "current_pin", lambda: None)


# ── schema_version: anything but the pin is refused whole ─────────────────────────────


@pytest.mark.parametrize("version", [None, 2, 4, 999, True, "3"])
def test_indices_refuse_off_contract_artifact(version, caplog):
    art = _intraday(version)
    if version is None:
        del art["schema_version"]
    snap = indices.load_indices(reader=lambda: art, now=_NOW)
    assert snap.available is False and snap.indices == []
    assert snap.rejected_reason and "schema_version" in snap.rejected_reason
    assert indices.proxy_day_return("VOO", reader=lambda: art, now=_NOW) is None
    assert any(r.levelname == "ERROR" for r in caplog.records)


@pytest.mark.parametrize("version", [None, 999])
def test_intraday_overlay_refuses_off_contract_artifact(version):
    art = _intraday(version)
    if version is None:
        del art["schema_version"]
    assert intraday.load_quotes(reader=lambda: art, now=_NOW) == ({}, None, True)


def test_pinned_version_still_reads():
    snap = indices.load_indices(reader=lambda: _intraday(), now=_NOW)
    assert snap.available and snap.rejected_reason is None and snap.invalid_fields == {}
    assert snap.indices[0].change == pytest.approx(1.0)
    assert indices.proxy_day_return("VOO", reader=lambda: _intraday(), now=_NOW) == pytest.approx(0.02)
    quotes, as_of, _stale = intraday.load_quotes(reader=lambda: _intraday(), now=_NOW)
    assert "AAPL" in quotes and as_of == "2026-09-14T14:58:00Z"


def test_technical_rating_refuses_each_artifact_independently():
    snap = technical_rating.load_technical_rating(
        intraday_reader=lambda: _ratings(999), technicals_reader=lambda: _technicals(), now=_NOW
    )
    assert set(snap.by_symbol) == {"MSFT"}  # EOD fallback still covers its own symbols
    assert set(snap.rejected) == {technical_rating.TECHNICAL_RATINGS_KEY}

    snap = technical_rating.load_technical_rating(
        intraday_reader=lambda: _ratings(), technicals_reader=lambda: _technicals(3), now=_NOW
    )
    assert set(snap.by_symbol) == {"AAPL"}
    assert set(snap.rejected) == {technical_rating.TECHNICALS_KEY}


def test_rating_performance_refuses_boolean_version():
    # `True == 1` in Python: the old `!=` check admitted it as schema_version 1.
    assert technical_rating_performance.load_rating_performance(reader=lambda: _perf(True)) is None
    assert technical_rating_performance.load_rating_performance(reader=lambda: _perf()) is not None


# ── non-finite / garbage values read as unavailable, never as a number ────────────────


@pytest.mark.parametrize("bad", [NAN, INF, -INF, "NaN", "Infinity", "garbage", True])
def test_indices_non_finite_quote_is_unavailable(bad):
    snap = indices.load_indices(reader=lambda: _intraday(quote={"last": bad}), now=_NOW)
    q = snap.indices[0]
    assert q.last is None and q.change is None and q.change_pct is None
    assert q.prev_close == 100.0
    assert snap.invalid_fields == {"SPY": ("last",)}


@pytest.mark.parametrize("bad", [NAN, INF, "NaN"])
def test_proxy_day_return_non_finite_is_none(bad):
    assert indices.proxy_day_return("VOO", reader=lambda: _intraday(proxy={"prev_close": bad}), now=_NOW) is None


@pytest.mark.parametrize("bad", [NAN, INF, "-Infinity", "garbage"])
def test_intraday_overlay_never_values_a_position_at_a_non_finite_last(db_session, bad):
    prices, estimated = intraday._overlay(
        db_session, ["AAPL"], {"AAPL": {"last": bad, "session_date": _SESSION}}, today=date(2026, 9, 14), now=_NOW
    )
    assert "AAPL" not in prices and not estimated


@pytest.mark.parametrize("bad", [NAN, INF, "NaN"])
def test_intraday_day_move_helper_rejects_non_finite(bad):
    assert intraday._f({"last": bad}, "last") is None


@pytest.mark.parametrize("bad", [NAN, INF, "Infinity"])
def test_technical_rating_non_finite_score_is_unavailable(bad):
    snap = technical_rating.load_technical_rating(
        intraday_reader=lambda: _ratings(score=bad, n_sell=INF),
        technicals_reader=lambda: _technicals(osc_score=bad),
        now=_NOW,
    )
    aapl, msft = snap.by_symbol["AAPL"], snap.by_symbol["MSFT"]
    assert aapl.score is None and aapl.label == "Buy" and aapl.n_buy == 9 and aapl.n_sell is None
    assert msft.osc_score is None and msft.score == -0.2
    assert snap.invalid_fields == {"AAPL": ("score",), "MSFT": ("osc_score",)}


@pytest.mark.parametrize("bad", [NAN, INF, "NaN"])
def test_rating_performance_non_finite_stat_is_unavailable(bad):
    perf = technical_rating_performance.load_rating_performance(reader=lambda: _perf(mean_fwd=bad))
    bucket = perf.bucket_for_label("Buy", segment="all", window=20, horizon=5)
    assert bucket.mean_fwd is None and bucket.n == 10
    for v in (bucket.mean_fwd, bucket.hit_rate):
        assert v is None or math.isfinite(v)
