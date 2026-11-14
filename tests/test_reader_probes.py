"""Closes-when probes for metron-ops-I373: wrong units, and stale content with a recent write.

Both go through the REAL readers (`load_fundamentals`, `load_technicals` and the S3
default path), not through a schema helper. A wrong-unit value must read as invalid (named
in ``invalid_fields``, never a metric); content older than its limit must carry an explicit
``stale_reason`` even when the object was written moments ago.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from api.services import fundamentals, spine_artifacts, technicals
from portfolio_analytics.admission import content_staleness, read_number

_NOW = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)
_TODAY = "2026-11-13"


def _fund(**fields: object) -> dict:
    return {"schema_version": 5, "as_of": _TODAY, "source": "yfinance", "fundamentals": {"AAPL": fields}}


def _tech(**fields: object) -> dict:
    return {"schema_version": 4, "as_of": _TODAY, "source": "ohlc", "technicals": {"AAPL": fields}}


# ── (a) wrong units ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("rsi_14", 150.0),
        ("pct_in_52w_range", 55.0),  # a percent where a 0-1 fraction is declared
        ("pct_to_ma_50", 12.0),  # 12 (percent) where a fraction (-1..5) is declared
        ("pct_from_52wk_high", -35.0),
        ("mom_20d", 8.0),
        ("mom_60d", -40.0),
    ],
)
def test_technicals_value_in_the_wrong_unit_is_invalid_not_a_metric(field, value):
    snap = technicals.load_technicals(reader=lambda: _tech(**{field: value}), now=_NOW)
    reading = getattr(snap.by_symbol["AAPL"], field)
    assert reading is None
    assert snap.invalid_fields == {"AAPL": (field,)}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("grossMargins", 45.0),  # percent where a fraction is declared
        ("operatingMargins", 30.0),
        ("dividendYield", 450.0),  # a yield above 100 percent
        ("dividendYield", -1.0),
    ],
)
def test_fundamentals_value_in_the_wrong_unit_is_invalid_not_a_metric(field, value):
    snap = fundamentals.load_fundamentals(reader=lambda: _fund(**{field: value}), now=_NOW)
    row = snap.by_symbol["AAPL"]
    attr = {"grossMargins": "gross_margins", "operatingMargins": "operating_margins", "dividendYield": "dividend_yield"}[field]
    assert getattr(row, attr) is None
    assert snap.invalid_fields == {"AAPL": (field,)}


def test_declared_unit_values_pass_unchanged():
    snap = fundamentals.load_fundamentals(
        reader=lambda: _fund(grossMargins=0.45, operatingMargins=-3.06, dividendYield=4.5), now=_NOW
    )
    row = snap.by_symbol["AAPL"]
    assert (row.gross_margins, row.operating_margins) == (0.45, -3.06)
    assert row.dividend_yield == pytest.approx(0.045)  # percent -> fraction, as before
    assert not snap.invalid_fields
    tech = technicals.load_technicals(
        reader=lambda: _tech(rsi_14=96.33, pct_in_52w_range=1.0, pct_from_52wk_high=0.0, mom_60d=0.67), now=_NOW
    )
    assert tech.by_symbol["AAPL"].rsi_14 == 96.33
    assert not tech.invalid_fields


def test_a_wrong_unit_value_does_not_poison_its_neighbours():
    snap = technicals.load_technicals(reader=lambda: _tech(rsi_14=55.0, mom_20d=8.0), now=_NOW)
    assert snap.by_symbol["AAPL"].rsi_14 == 55.0
    assert snap.invalid_fields == {"AAPL": ("mom_20d",)}


def test_read_number_bounds_are_inclusive():
    assert read_number(1.0, lo=0.0, hi=1.0) == 1.0
    assert read_number(0.0, lo=0.0, hi=1.0) == 0.0
    assert read_number(1.0001, lo=0.0, hi=1.0) != 1.0001


# ── (b) stale content with a recent write ───────────────────────────────────


def _s3_object(art: dict, *, written: datetime) -> MagicMock:
    s3 = MagicMock()
    body = MagicMock()
    body.read.return_value = json.dumps(art).encode()
    s3.get_object.return_value = {"Body": body, "VersionId": "v1", "ETag": '"abc"', "LastModified": written}
    return s3


@pytest.fixture
def _spine(monkeypatch):
    spine_artifacts.clear()

    def install(art: dict, *, written: datetime) -> None:
        import boto3

        monkeypatch.setattr(boto3, "client", lambda *_a, **_k: _s3_object(art, written=written))

    yield install
    spine_artifacts.clear()


@pytest.mark.parametrize(
    ("loader", "art_fn"),
    [(fundamentals.load_fundamentals, _fund), (technicals.load_technicals, _tech)],
)
def test_old_content_written_recently_is_flagged_stale_through_the_real_s3_reader(loader, art_fn, _spine):
    art = art_fn()
    art["as_of"] = "2026-10-01"  # six weeks old content ...
    _spine(art, written=_NOW - timedelta(minutes=3))  # ... re-uploaded three minutes ago
    snap = loader(now=_NOW)
    assert snap.stale_reason is not None
    assert "2026-10-01" in snap.stale_reason
    assert "recent write of old content" in snap.stale_reason
    assert snap.input_source is not None and snap.input_source.last_modified == _NOW - timedelta(minutes=3)
    assert "AAPL" in snap.by_symbol, "stale is flagged, not dropped: the caller decides how to render it"


@pytest.mark.parametrize(
    ("loader", "art_fn"),
    [(fundamentals.load_fundamentals, _fund), (technicals.load_technicals, _tech)],
)
def test_current_content_is_not_stale(loader, art_fn, _spine):
    _spine(art_fn(), written=_NOW - timedelta(hours=9))
    assert loader(now=_NOW).stale_reason is None


@pytest.mark.parametrize("as_of", [None, "", "not-a-date"])
def test_content_with_no_usable_as_of_is_stale_because_its_age_is_unknown(as_of):
    art = _fund()
    art["as_of"] = as_of
    reason = fundamentals.load_fundamentals(reader=lambda: art, now=_NOW).stale_reason
    assert reason is not None and "no usable as_of" in reason


def test_staleness_is_judged_on_content_date_not_write_time():
    old = date(2026, 10, 1)
    fresh_write = content_staleness(old, now=_NOW, written_at=_NOW - timedelta(minutes=1))
    old_write = content_staleness(old, now=_NOW, written_at=_NOW - timedelta(days=40))
    assert fresh_write is not None and "recent write of old content" in fresh_write
    assert old_write is not None and "recent write" not in old_write
    assert content_staleness(date(2026, 11, 9), now=_NOW) is None  # 5 days: a long weekend
    assert content_staleness(date(2026, 11, 8), now=_NOW) is not None  # 6 days


def test_a_refused_artifact_is_not_also_judged_for_staleness():
    art = _fund()
    art["schema_version"] = 999
    snap = fundamentals.load_fundamentals(reader=lambda: art, now=_NOW)
    assert snap.rejected_reason is not None and snap.stale_reason is None
