"""Holdings-table metrics consumer (Holdings metrics) — fundamentals v2 (P/B + P/S), the
technicals service, the valuation-medians service, and the per-holding enrichment mapping.

These pin the contract Metron consumes from the data spine: artifact field → dataclass →
Holding field. Pure unit tests (injected readers / monkeypatched yf map — no S3, no DB).
"""

from __future__ import annotations

from api.routers import portfolios
from api.services import analytics, fundamentals, technicals, valuation_medians

# ── fundamentals v2 ──────────────────────────────────────────────────────────

def test_fundamentals_parses_pb_and_ps():
    art = {
        "as_of": "2026-06-26",
        "fundamentals": {
            "AAPL": {"trailingPE": 30.0, "priceToBook": 6.0, "priceToSalesTrailing12Months": 7.5,
                     "dividendYield": 0.5, "sector": "Technology"},
        },
    }
    snap = fundamentals.load_fundamentals(reader=lambda: art)
    f = snap.by_symbol["AAPL"]
    assert f.price_to_book == 6.0 and f.price_to_sales == 7.5
    assert f.dividend_yield == 0.005  # percent → fraction


def test_fundamentals_missing_pb_ps_is_none():
    snap = fundamentals.load_fundamentals(reader=lambda: {"fundamentals": {"X": {"trailingPE": 12.0}}})
    f = snap.by_symbol["X"]
    assert f.price_to_book is None and f.price_to_sales is None


def test_fundamentals_parses_balance_sheet():
    art = {"fundamentals": {"AAPL": {"totalDebt": 1.1e11, "totalCash": 6.0e10,
                                     "ebitda": 1.3e11, "freeCashflow": 9.0e10}}}
    f = fundamentals.load_fundamentals(reader=lambda: art).by_symbol["AAPL"]
    assert f.total_debt == 1.1e11 and f.total_cash == 6.0e10
    assert f.ebitda == 1.3e11 and f.free_cashflow == 9.0e10


# ── technicals ───────────────────────────────────────────────────────────────

def test_technicals_round_trip():
    art = {
        "as_of": "2026-06-26",
        "technicals": {
            "AAPL": {"rsi_14": 61.2, "macd_hist": 1.3, "ma_50": 190.0, "ma_200": 175.0,
                     "pct_to_ma_50": 0.05, "pct_to_ma_200": 0.12, "high_52w": 210.0,
                     "low_52w": 150.0, "pct_in_52w_range": 0.83, "mom_20d": 0.04, "mom_60d": 0.09},
        },
    }
    snap = technicals.load_technicals(reader=lambda: art)
    t = snap.by_symbol["AAPL"]
    assert t.rsi_14 == 61.2 and t.pct_in_52w_range == 0.83 and t.mom_20d == 0.04
    assert str(snap.as_of) == "2026-06-26"


def test_technicals_missing_artifact_is_empty():
    snap = technicals.load_technicals(reader=lambda: None)
    assert snap.by_symbol == {} and snap.as_of is None


# ── valuation medians ────────────────────────────────────────────────────────

def test_valuation_medians_parse_and_normalize():
    art = {
        "as_of": "2026-06-26",
        "by_sector": {"Technology": {"n": 152, "trailing_pe": 28.0, "price_to_book": 6.2,
                                     "dividend_yield": 1.2}},
        "by_country": {"United States": {"n": 800, "trailing_pe": 22.0}},
    }
    snap = valuation_medians.load_valuation_medians(reader=lambda: art)
    tech = snap.by_sector["Technology"]
    assert tech.n == 152 and tech.trailing_pe == 28.0 and tech.price_to_book == 6.2
    assert tech.dividend_yield == 0.012  # percent → fraction, matches per-holding div_yield
    assert snap.by_country["United States"].n == 800


# ── per-holding enrichment mapping ───────────────────────────────────────────

def test_enrich_metrics_maps_fundamentals_and_technicals(monkeypatch):
    held = [analytics.Holding(ticker="AAPL", quantity=1, avg_cost=1, cost_basis=1),
            analytics.Holding(ticker="ZZZ", quantity=1, avg_cost=1, cost_basis=1)]  # no spine data

    # Capture the real loaders before patching (the patched attr shadows the module fn).
    real_funds, real_techs = fundamentals.load_fundamentals, technicals.load_technicals
    monkeypatch.setattr(portfolios.tearsheet_service, "_yf_symbol_map",
                        lambda session, syms: {"AAPL": "AAPL", "ZZZ": "ZZZ"})
    monkeypatch.setattr(
        portfolios.fundamentals_service, "load_fundamentals",
        lambda: real_funds(reader=lambda: {
            "fundamentals": {"AAPL": {"trailingPE": 30.0, "forwardPE": 25.0, "priceToBook": 6.0,
                                      "priceToSalesTrailing12Months": 7.5, "marketCap": 3.0e12,
                                      "returnOnEquity": 0.5, "debtToEquity": 150.0, "beta": 1.2,
                                      "grossMargins": 0.45, "revenueGrowth": 0.08,
                                      "totalDebt": 1.2e11, "totalCash": 4.0e10, "ebitda": 1.0e11,
                                      "freeCashflow": 9.0e10, "quickRatio": 0.9}}}),
    )
    monkeypatch.setattr(
        portfolios.technicals_service, "load_technicals",
        lambda: real_techs(reader=lambda: {
            "technicals": {"AAPL": {"rsi_14": 61.0, "macd_hist": 1.1, "pct_to_ma_50": 0.05,
                                    "pct_to_ma_200": 0.12, "pct_in_52w_range": 0.8, "mom_20d": 0.03}}}),
    )

    portfolios._enrich_metrics(session=None, held=held)

    aapl = held[0]
    assert aapl.pe == 30.0 and aapl.fwd_pe == 25.0 and aapl.pb == 6.0 and aapl.ps == 7.5
    assert aapl.market_cap == 3.0e12 and aapl.roe == 0.5 and aapl.beta == 1.2
    assert aapl.rsi_14 == 61.0 and aapl.pct_in_52w_range == 0.8 and aapl.mom_20d == 0.03
    # Balance sheet: absolute balances mapped, net debt + leverage derived.
    assert aapl.cash == 4.0e10 and aapl.debt == 1.2e11 and aapl.quick_ratio == 0.9
    assert aapl.net_debt == 1.2e11 - 4.0e10
    assert aapl.net_debt_to_ebitda == (1.2e11 - 4.0e10) / 1.0e11
    # A holding absent from both artifacts keeps all metrics None (coverage gap, not zeros).
    zzz = held[1]
    assert zzz.pe is None and zzz.rsi_14 is None and zzz.market_cap is None
