"""Market board (metron-ops-I304, Stage A: Held/Watchlist scopes only — R5 2026-09-15
moved Universe to Stage B). Router tests: one per scope, the beta 404, an invalid-scope
422, sort-by-score-only (no position-weight sort), and per-row as-of.
"""

from __future__ import annotations

import io
import uuid
from datetime import UTC, datetime

import pytest

from api.config import settings
from api.routers import market_board
from api.services import technical_rating

CSV = (
    "date,type,symbol,quantity,price,amount,account\n"
    "2024-01-02,BUY,AAPL,10,100,1000,Brokerage\n"
    "2024-01-02,BUY,MSFT,5,300,1500,Brokerage\n"
)


def _rating_art() -> dict:
    # ``load_technical_rating`` (no ``now=`` override) judges freshness against the REAL
    # wall clock — anchor ``as_of_utc`` to "now" (bugclass_a_test_fixture_that_expires_at_
    # utc_midnight), never a hardcoded date.
    return {
        "schema_version": 1,
        "as_of_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "ratings": {
            "AAPL": {
                "score": 0.4, "label": "Buy", "ma_score": 0.5, "osc_score": 0.3,
                "n_buy": 8, "n_neutral": 2, "n_sell": 1, "n_votes": 11,
            },
            "MSFT": {
                "score": 0.8, "label": "Strong Buy", "ma_score": 0.9, "osc_score": 0.7,
                "n_buy": 10, "n_neutral": 1, "n_sell": 0, "n_votes": 11,
            },
            "NVDA": {
                "score": -0.6, "label": "Sell", "ma_score": -0.5, "osc_score": -0.7,
                "n_buy": 1, "n_neutral": 1, "n_sell": 9, "n_votes": 11,
            },
        },
    }


@pytest.fixture()
def tenant():
    return str(uuid.uuid4())


def _hdr(tenant):
    return {"X-Tenant-Id": tenant}


def _seed(client, tenant):
    pid = client.post("/portfolios", json={"name": "P"}, headers=_hdr(tenant)).json()["id"]
    r = client.post(
        f"/portfolios/{pid}/import/csv",
        files={"file": ("t.csv", io.BytesIO(CSV.encode()), "text/csv")},
        headers=_hdr(tenant),
    )
    assert r.status_code == 200
    return pid


def _patch_ratings(monkeypatch):
    real_load = technical_rating.load_technical_rating
    monkeypatch.setattr(
        market_board.technical_rating_service, "load_technical_rating",
        lambda: real_load(intraday_reader=_rating_art),
    )


def test_held_scope_rates_positions_sorted_by_score(client, tenant, monkeypatch):
    monkeypatch.setattr(settings, "tier_simulator", False)
    monkeypatch.setattr(settings, "feed_entitled", True)
    _patch_ratings(monkeypatch)
    pid = _seed(client, tenant)

    r = client.get(f"/portfolios/{pid}/market-board", params={"scope": "held"}, headers=_hdr(tenant))
    assert r.status_code == 200
    body = r.json()
    assert body["scope"] == "held"
    symbols = [row["symbol"] for row in body["rows"]]
    assert set(symbols) == {"AAPL", "MSFT"}
    # MSFT (score 0.8) ranks above AAPL (score 0.4) — sorted by score, never by the
    # position's dollar weight (a 5-share MSFT line outranks a 10-share AAPL line here).
    assert symbols == ["MSFT", "AAPL"]
    for row in body["rows"]:
        assert row["held"] is True
        assert row["basis"] == "intraday"
        assert row["as_of"] is not None


def test_watchlist_scope_includes_unheld_and_held_flag(client, tenant, monkeypatch):
    monkeypatch.setattr(settings, "tier_simulator", False)
    monkeypatch.setattr(settings, "feed_entitled", True)
    _patch_ratings(monkeypatch)
    pid = _seed(client, tenant)
    client.post(f"/portfolios/{pid}/watchlist", json={"symbol": "NVDA"}, headers=_hdr(tenant))
    client.post(f"/portfolios/{pid}/watchlist", json={"symbol": "AAPL"}, headers=_hdr(tenant))

    r = client.get(f"/portfolios/{pid}/market-board", params={"scope": "watchlist"}, headers=_hdr(tenant))
    assert r.status_code == 200
    body = r.json()
    assert body["scope"] == "watchlist"
    by_symbol = {row["symbol"]: row for row in body["rows"]}
    assert set(by_symbol) == {"NVDA", "AAPL"}
    # AAPL is on the watchlist AND held; NVDA is watchlist-only.
    assert by_symbol["AAPL"]["held"] is True
    assert by_symbol["NVDA"]["held"] is False
    # Sorted by score: AAPL (0.4) ranks above NVDA (-0.6).
    assert [row["symbol"] for row in body["rows"]] == ["AAPL", "NVDA"]


def test_beta_no_feed_build_404s(client, tenant, monkeypatch):
    """The no-feed beta must not learn the board exists — 404, never an empty table read
    as 'nothing attractive' (metron-ops#52 precedent, deploy-cash)."""
    monkeypatch.setattr(settings, "tier_simulator", False)
    monkeypatch.setattr(settings, "feed_entitled", False)

    def _boom():
        raise AssertionError("technical_rating reader must not be called off a feed-entitled build")

    monkeypatch.setattr(market_board.technical_rating_service, "load_technical_rating", _boom)
    pid = _seed(client, tenant)

    r = client.get(f"/portfolios/{pid}/market-board", params={"scope": "held"}, headers=_hdr(tenant))
    assert r.status_code == 404


def test_universe_scope_is_stage_b_not_a_placeholder(client, tenant, monkeypatch):
    """R5 (2026-09-15): the universe scope moved entirely to Stage B — a request for it
    422s rather than rendering a 'not yet published' placeholder (which the ORIGINAL issue
    spec called for before the scope change)."""
    monkeypatch.setattr(settings, "tier_simulator", False)
    monkeypatch.setattr(settings, "feed_entitled", True)
    pid = _seed(client, tenant)

    r = client.get(f"/portfolios/{pid}/market-board", params={"scope": "universe"}, headers=_hdr(tenant))
    assert r.status_code == 422


def test_unrated_symbol_sorts_last_never_as_zero(client, tenant, monkeypatch):
    monkeypatch.setattr(settings, "tier_simulator", False)
    monkeypatch.setattr(settings, "feed_entitled", True)
    # A ratings artifact that covers only AAPL — MSFT has no rating. Capture the real
    # loader BEFORE patching — market_board.technical_rating_service IS the
    # technical_rating module (same object, imported under an alias), so patching its
    # load_technical_rating attribute would otherwise shadow the very function this
    # lambda calls into (self-reference — same gotcha test_holdings_technical_rating_
    # gate.py notes).
    real_load = technical_rating.load_technical_rating
    monkeypatch.setattr(
        market_board.technical_rating_service, "load_technical_rating",
        lambda: real_load(intraday_reader=lambda: {
            "schema_version": 1,
            "as_of_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "ratings": {"AAPL": {"score": -0.5, "label": "Sell", "ma_score": -0.4, "osc_score": -0.6}},
        }),
    )
    pid = _seed(client, tenant)

    r = client.get(f"/portfolios/{pid}/market-board", params={"scope": "held"}, headers=_hdr(tenant))
    body = r.json()
    # AAPL has a real (negative) score; MSFT has none. Unrated must still sort AFTER a
    # rated-but-negative row, never ahead of it as if None == 0.
    assert [row["symbol"] for row in body["rows"]] == ["AAPL", "MSFT"]
    msft = next(row for row in body["rows"] if row["symbol"] == "MSFT")
    assert msft["score"] is None
    assert msft["label"] is None
