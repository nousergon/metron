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
        ("weird-thing", "X", None, "other"),
        # Fixed-income family split (metron-ops#114): treasury / cd / generic bond.
        ("bond", "912828XG8", "US Treasury Note 2.5%", "treasury"),  # name + 912 CUSIP → treasury
        ("bond", "459200AB1", "IBM Corp 3.45% 2026", "bond"),  # corporate → generic bond
        ("treasury", "912796YR3", "US T-Bill", "treasury"),  # asset_class treasury
        ("cd", "06051GFN4", "Goldman Sachs Bank CD 5%", "cd"),  # asset_class cd
        (None, "912810TM0", "US Treasury Bond", "treasury"),  # 912 CUSIP prefix → treasury
        (None, "037833100", "Apple Inc 3.85% Note", "bond"),  # non-912 CUSIP → generic bond
        (None, "Brokered CD 4.5%", "Marcus Bank CD", "cd"),  # name CD token → cd
        # Inference when asset_class is missing.
        (None, "12345678", None, "equity"),  # 8 digits — not a CUSIP
        (None, "VMFXX", "Vanguard Money Market", "cash"),
        (None, "AAPL", "Apple Inc", "equity"),  # normal alpha ticker → equity
        ("", "AAPL", None, "equity"),  # empty asset_class treated as missing
    ],
)
def test_classify(asset_class, ticker, name, expected):
    assert classify_security_type(asset_class, ticker, name) == expected
