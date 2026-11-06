"""Runtime admission for the derived per-symbol spine readers (metron-ops-I373).

`test_reader_admission.py` covers fundamentals, technicals and the price/FX spine. The same
gap remained in the security-performance, valuation-medians, analyst and sentiment readers:
none looked at ``schema_version``, and each converted numbers with a bare ``float()``, so a
``schema_version: 999`` artifact carrying ``"nan"`` / ``"Infinity"`` came back as a NaN YTD
return or an infinite sector median. These cases go through the REAL readers.
"""

from __future__ import annotations

import math

import pytest

from api.services import analyst, security_performance, sentiment, valuation_medians

NAN, INF = float("nan"), float("inf")


def _perf(version=1, **row):
    return {"schema_version": version, "as_of": "2026-11-04", "source": "t",
            "performance": {"AAPL": {"ytd_pct": 0.1, "period_returns": {"1Y": 0.2}, **row}}}


def _medians(version=1, **row):
    return {"schema_version": version, "as_of": "2026-11-04", "source": "t",
            "by_sector": {"Technology": {"n": 70, "trailing_pe": 30.0, **row}},
            "by_country": {"US": {"n": 1500, "trailing_pe": 21.0}}}


def _analyst(version=1, **row):
    return {"schema_version": version, "as_of": "2026-11-04", "source": "t",
            "analyst": {"AAPL": {"consensus_rating": "buy", "mean_target": 250.0, **row}}}


def _sentiment(version=1, **row):
    return {"schema_version": version, "as_of": "2026-11-04", "source": "t",
            "sentiment": {"AAPL": {"sentiment": 0.3, "n_articles": 12, **row}}}


# ── unknown / missing schema_version: refused whole, with a reason ──────────────────────

@pytest.mark.parametrize("version", [999, None, "1", 1.0, True])
def test_security_performance_refuses_off_contract_schema(version):
    art = _perf(version, ytd_pct="nan")
    if version is None:
        del art["schema_version"]
    snap = security_performance.load_security_performance(reader=lambda: art)
    assert snap.by_symbol == {}
    assert snap.as_of is None
    assert snap.rejected_reason and "schema_version" in snap.rejected_reason


def test_valuation_medians_refuses_unknown_schema():
    snap = valuation_medians.load_valuation_medians(reader=lambda: _medians(999))
    assert snap.by_sector == {} and snap.by_country == {}
    assert snap.rejected_reason


def test_analyst_refuses_unknown_schema():
    snap = analyst.load_analyst(reader=lambda: _analyst(2))
    assert snap.by_symbol == {} and snap.rejected_reason


def test_sentiment_refuses_unknown_schema():
    snap = sentiment.load_sentiment(reader=lambda: _sentiment(0))
    assert snap.by_symbol == {} and snap.rejected_reason


def test_absent_artifact_is_not_a_refusal():
    for snap in (
        security_performance.load_security_performance(reader=lambda: None),
        valuation_medians.load_valuation_medians(reader=lambda: None),
        analyst.load_analyst(reader=lambda: None),
        sentiment.load_sentiment(reader=lambda: None),
    ):
        assert snap.rejected_reason is None


# ── non-finite / garbage numbers: unavailable and named, never a metric ─────────────────

@pytest.mark.parametrize("bad", ["nan", NAN, "Infinity", INF, -INF, "abc", True])
def test_security_performance_non_finite_is_unavailable_and_named(bad):
    art = _perf(ytd_pct=bad, sharpe=bad, period_returns={"1Y": 0.2, "3Y": bad})
    snap = security_performance.load_security_performance(reader=lambda: art)
    row = snap.by_symbol["AAPL"]
    assert row.ytd_pct is None and row.sharpe is None
    assert row.period_returns == {"1Y": 0.2}
    assert set(snap.invalid_fields["AAPL"]) == {"ytd_pct", "sharpe", "period_returns.3Y"}


def test_valuation_medians_non_finite_is_unavailable_and_named():
    snap = valuation_medians.load_valuation_medians(
        reader=lambda: _medians(trailing_pe="Infinity", dividend_yield=NAN))
    tech = snap.by_sector["Technology"]
    assert tech.trailing_pe is None and tech.dividend_yield is None
    assert set(snap.invalid_fields["by_sector.Technology"]) == {"trailing_pe", "dividend_yield"}
    assert snap.by_country["US"].trailing_pe == 21.0


def test_analyst_non_finite_is_unavailable_and_named():
    snap = analyst.load_analyst(reader=lambda: _analyst(mean_target="nan", rating_score=INF))
    row = snap.by_symbol["AAPL"]
    assert row.mean_target is None and row.rating_score is None
    assert row.target_upside(200.0) is None
    assert set(snap.invalid_fields["AAPL"]) == {"mean_target", "rating_score"}


def test_sentiment_non_finite_is_unavailable_and_named():
    snap = sentiment.load_sentiment(reader=lambda: _sentiment(sentiment="nan"))
    assert snap.by_symbol["AAPL"].sentiment is None
    assert snap.invalid_fields["AAPL"] == ("sentiment",)


# ── on-contract artifacts read unchanged ───────────────────────────────────────────────

def test_on_contract_artifacts_read_unchanged():
    perf = security_performance.load_security_performance(reader=lambda: _perf())
    assert perf.by_symbol["AAPL"].ytd_pct == 0.1 and perf.invalid_fields == {}
    med = valuation_medians.load_valuation_medians(reader=lambda: _medians(dividend_yield=1.5))
    assert math.isclose(med.by_sector["Technology"].dividend_yield, 0.015)
    assert med.rejected_reason is None and med.invalid_fields == {}
    assert analyst.load_analyst(reader=lambda: _analyst()).by_symbol["AAPL"].mean_target == 250.0
    assert sentiment.load_sentiment(reader=lambda: _sentiment()).by_symbol["AAPL"].sentiment == 0.3
