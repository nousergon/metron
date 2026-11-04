"""The Holdings market note (api/services/market_note.py)."""

from __future__ import annotations

import uuid
from datetime import date

from api.services import market_note
from api.services.attribution import AttributionSummary, SectorEffect
from api.services.benchmark_gap import BenchmarkGapSummary, GapDriver
from api.services.performance import BenchmarkReturn, PeriodTile

SESSION = date(2026, 11, 3)


def _tile(twr=-0.008, spy=0.006, others=True):
    benches = [BenchmarkReturn("SPY", "S&P 500", spy, None if spy is None else twr - spy)]
    if others:
        benches += [BenchmarkReturn("QQQ", "Nasdaq 100", 0.009, twr - 0.009),
                    BenchmarkReturn("IWM", "Russell 2000", None, None)]
    return PeriodTile("today", "Today", date(2026, 11, 2), SESSION, -1234.0, twr, benchmarks=benches)


def _energy_attr(gap=-0.014):
    return AttributionSummary(
        computable=True, active_return=gap, start_date=date(2026, 11, 2), as_of=SESSION,
        sectors=[
            SectorEffect("Energy", 0.18, 0.03, -0.021, -0.015, allocation=-0.009,
                         selection=-0.002, interaction=-0.001, total=-0.012),
            SectorEffect("Technology", 0.25, 0.32, 0.011, 0.010, allocation=-0.001,
                         selection=0.0, interaction=0.0, total=-0.001),
        ])


def _gap():
    return BenchmarkGapSummary(computable=True, drivers=[
        GapDriver("XOM", "overweight", 0.08, 0.01, 0.07, -0.025, -0.002, -0.0003, -0.0018),
        GapDriver("NVDA", "underweight", 0.02, 0.07, -0.05, 0.03, 0.0006, 0.0021, -0.0015),
        GapDriver("CVX", "overweight", 0.05, 0.01, 0.04, -0.02, -0.001, -0.0002, -0.0008, earnings=True),
        GapDriver("TSLA", "not_held", 0.0, 0.02, -0.02, -0.04, 0.0, -0.0008, 0.0008),
    ])


def test_the_headline_names_the_gap_and_the_other_indexes():
    note = market_note.compose(_tile())
    assert note.available
    assert note.sentences[0] == (
        "On Tuesday Nov 3 your portfolio fell 0.8% while the S&P 500 rose 0.6%, "
        "behind by 1.4 points (Nasdaq 100 +0.9%).")
    assert round(note.gap, 4) == -0.014


def test_the_sector_explaining_most_of_the_gap_is_named_with_how():
    note = market_note.compose(_tile(), _energy_attr())
    assert note.sentences[1] == (
        "Most of the gap came from Energy: you are overweight (18% of the portfolio vs 3% of "
        "the S&P 500) and the sector fell 1.5%, which hurt by 1.2 points.")


def test_selection_led_sector_says_your_holdings():
    attr = _energy_attr()
    attr.sectors[0].allocation, attr.sectors[0].selection = -0.001, -0.010
    note = market_note.compose(_tile(), attr)
    assert note.sentences[1].startswith("Most of the gap came from your Energy holdings, which fell 2.1%")


def test_names_on_both_sides_with_their_tags():
    note = market_note.compose(_tile(), _energy_attr(), _gap())
    assert note.sentences[2] == (
        "Biggest drags: XOM (−2.5%, overweight), NVDA (+3.0%, underweight); "
        "biggest help: TSLA (−4.0%, not held).")


def test_a_leg_that_cannot_be_computed_is_named_not_guessed():
    attr = AttributionSummary(computable=False, reason="No priced holdings — refresh prices first.")
    gap = BenchmarkGapSummary(computable=False, reason="index artifact missing")
    note = market_note.compose(_tile(), attr, gap)
    assert len(note.sentences) == 1
    assert note.missing == ["sector view: No priced holdings — refresh prices first.",
                            "names: index artifact missing"]


def test_in_line_and_no_benchmark():
    assert "in line with the S&P 500" in market_note.compose(_tile(0.006, 0.0061)).sentences[0]
    note = market_note.compose(_tile(0.004, None, others=False))
    assert note.sentences == ["On Tuesday Nov 3 your portfolio rose 0.4%."]


def test_no_session_yet_is_not_available():
    tile = PeriodTile("today", "Today", None, None, None, None, note="as of Nov 2")
    note = market_note.compose(tile)
    assert not note.available and note.missing == ["as of Nov 2"]


def test_endpoint_answers_with_the_note_shape(client, monkeypatch):
    tenant = str(uuid.uuid4())
    monkeypatch.setattr("api.services.prices.fetch_close_history", lambda *a, **k: {})
    pid = client.post("/portfolios", json={"name": "P"}, headers={"X-Tenant-Id": tenant}).json()["id"]
    r = client.get(f"/portfolios/{pid}/market-note", headers={"X-Tenant-Id": tenant})
    assert r.status_code == 200
    body = r.json()
    # No NAV history yet: honest "not available" with the reason, never an empty note.
    assert body["available"] is False and body["missing"]
