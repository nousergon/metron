"""Lot-level Open Position parsing (metron-ops#74).

With the Flex query's Open Positions → Lot detail enabled, IBKR emits both a SUMMARY
rollup and per-LOT rows. ``parse_open_lots`` reads only the LOT rows (each with an
``openDateTime``) so the historical position timeline can be reconstructed.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date

from portfolio_analytics.ingestion.ibkr_flex_connector import parse_open_lots

LOT_XML = """<FlexQueryResponse>
  <FlexStatements><FlexStatement><OpenPositions>
    <OpenPosition levelOfDetail="SUMMARY" accountId="U1" symbol="AAPL" position="10" costBasisMoney="1500" currency="USD" assetCategory="STK"/>
    <OpenPosition levelOfDetail="LOT" accountId="U1" symbol="AAPL" position="6" openDateTime="20250115;093000" costBasisMoney="900" currency="USD" assetCategory="STK"/>
    <OpenPosition levelOfDetail="LOT" accountId="U1" symbol="AAPL" position="4" openDateTime="20251219;031916" costBasisMoney="600" currency="USD" assetCategory="STK"/>
    <OpenPosition levelOfDetail="LOT" accountId="U1" symbol="CASHX" position="5" openDateTime="20250101" currency="USD" assetCategory="CASH"/>
    <OpenPosition levelOfDetail="LOT" accountId="U1" symbol="NODATE" position="3" currency="USD" assetCategory="STK"/>
  </OpenPositions></FlexStatement></FlexStatements>
</FlexQueryResponse>"""


def test_parse_open_lots_extracts_lot_rows_with_open_dates():
    lots = [lot for lot, _sec in parse_open_lots(ET.fromstring(LOT_XML))]
    # 2 AAPL lots kept; SUMMARY (not LOT), CASH (skip category), and the open-date-less
    # row are all dropped.
    assert len(lots) == 2
    by_date = {lot.open_date: lot for lot in lots}
    assert set(by_date) == {date(2025, 1, 15), date(2025, 12, 19)}
    early = by_date[date(2025, 1, 15)]
    assert early.ticker == "AAPL" and early.quantity == 6 and early.cost_basis == 900
    assert early.account_number == "U1" and early.currency == "USD"
