"""The mutual-fund → tracking-proxy ETF map (api/services/fund_proxy.py)."""

from __future__ import annotations

from api.services import fund_proxy


def test_known_funds_map_to_their_tracking_proxy():
    assert fund_proxy.proxy_for("FNILX") == "SPY"   # Fidelity ZERO Large Cap → S&P 500
    assert fund_proxy.proxy_for("FZILX") == "IXUS"  # Fidelity ZERO Intl → total intl ex-US
    assert fund_proxy.proxy_for("FTIHX") == "IXUS"  # Fidelity Total Intl → total intl ex-US


def test_unknown_fund_falls_back_to_broad_market():
    assert fund_proxy.proxy_for("SOMEFUND") == fund_proxy.DEFAULT_PROXY == "SPY"


def test_lookup_is_case_and_whitespace_insensitive():
    assert fund_proxy.proxy_for(" fnilx ") == "SPY"
    assert fund_proxy.proxy_for("fzilx") == "IXUS"


def test_proxy_etfs_set_covers_every_mapping_plus_default():
    assert fund_proxy.PROXY_ETFS == {"SPY", "IXUS"}
    assert set(fund_proxy.FUND_PROXY.values()) | {fund_proxy.DEFAULT_PROXY} == fund_proxy.PROXY_ETFS
