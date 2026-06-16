"""classify_security_type — the coarse asset-class bucket holdings group by (metron-ops#47).

The Security master's asset_class is authoritative; absent it, a 9-digit numeric symbol
is a CUSIP (bond/CD) and name keywords are the last resort, else equity.
"""

from __future__ import annotations

import pytest

from api.services.analytics import classify_security_type


@pytest.mark.parametrize(
    ("asset_class", "ticker", "name", "expected"),
    [
        # asset_class is authoritative (connector values are upper-cased at source, stored lower).
        ("equity", "AAPL", "Apple Inc", "equity"),
        ("STOCK", "AAPL", None, "equity"),
        ("etf", "SPY", "SPDR S&P 500", "etf"),
        ("fund", "VFIAX", "Vanguard 500", "fund"),
        ("option", "AAPL  240119C", None, "option"),
        ("cash", "USD", "US Dollar", "cash"),
        ("bond", "912828", "US Treasury", "bond"),
        ("weird-thing", "X", None, "other"),
        # Inference when asset_class is missing.
        (None, "037833100", "Apple bond", "bond"),  # 9-digit CUSIP → bond
        (None, "12345678", None, "equity"),  # 8 digits — not a CUSIP
        (None, "VMFXX", "Vanguard Money Market", "cash"),
        (None, "TLT", "20+ Year Treasury Bond", "bond"),
        (None, "AAPL", "Apple Inc", "equity"),  # normal alpha ticker → equity
        ("", "AAPL", None, "equity"),  # empty asset_class treated as missing
    ],
)
def test_classify(asset_class, ticker, name, expected):
    assert classify_security_type(asset_class, ticker, name) == expected
