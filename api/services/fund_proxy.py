"""Tracking-proxy ETFs for late-striking mutual funds.

A mutual fund prints its NAV once a day, hours AFTER Metron's EOD market-data run, so on
any given session its cached close lags by a session and the holding reads "flat." Left
alone that silently understates the portfolio's time-weighted return (a flat leg drags the
pooled NAV move toward zero). Two mechanisms address it (see api/services/performance.py):

  (A) reconcile/restate — the recorded NAV snapshot is marked PROVISIONAL while a fund leg
      is stale, then RESTATED with the fund's true struck NAV once it lands (next data run).
      Uses the fund's OWN struck close — no proxy, no estimation. Fixes historical TWR.
  (B) same-day estimate — for the LIVE Today tile, where the fund genuinely hasn't struck
      yet, its same-day return is estimated from a tracking-proxy ETF's same-day return,
      flagged "estimated" and reconciled away the next day by (A). This module is that map.

The proxies are deliberately explicit (not a beta/factor regression): the held funds are
index funds with a known 1:1 benchmark, so the tracking-proxy ETF's return is near-exact
(ρ>0.98) and fully auditable. The proxy ETFs are published to the data spine by
``alpha-engine-data`` (collectors/metron_market_data.py FUND_PROXY_ETFS); the cross-repo
drift guard keeps the two in lockstep.
"""

from __future__ import annotations

# Explicit per-fund tracking-proxy ETF. FNILX (Fidelity ZERO Large Cap) tracks a US large-cap
# index → SPY; FZILX (Fidelity ZERO International) and FTIHX (Fidelity Total International)
# track total-international-ex-US → IXUS (iShares, house-consistent with the style factors).
FUND_PROXY: dict[str, str] = {
    "FNILX": "SPY",
    "FZILX": "IXUS",
    "FTIHX": "IXUS",
}
# Broad-market fallback for an unmapped fund — never leave a fund un-estimated.
DEFAULT_PROXY = "SPY"

# The distinct proxy ETFs this module can resolve to — must be a subset of the spine's
# published FUND_PROXY_ETFS (asserted by the cross-repo drift guard).
PROXY_ETFS: set[str] = set(FUND_PROXY.values()) | {DEFAULT_PROXY}


def proxy_for(ticker: str) -> str:
    """The tracking-proxy ETF symbol for a (mutual-fund) ticker; broad-market default."""
    return FUND_PROXY.get((ticker or "").strip().upper(), DEFAULT_PROXY)
