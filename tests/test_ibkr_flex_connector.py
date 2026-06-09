"""IbkrFlexConnector + Flex section parser tests (PR3).

Validates the full canonical snapshot from a multi-section Activity Flex statement:
account NAV/cash (latest report date), open positions (skipping OPT/CASH), cash
transactions (dividend / interest-received / interest-paid / withholding mapping),
and realized lots. The fetch is injected (no network); bronze writes are isolated
to a tmp cwd. NOTE: only the realized `<Lot>` section is verified against a live
statement — the positions/cash/equity parsers are fixture-validated and need live
reconciliation before the IBKR cutover (PR4).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from portfolio_analytics.domain.ledger import TxnType
from portfolio_analytics.ingestion.ibkr_flex_connector import (
    IbkrFlexConnector,
    _map_cash_type,
    parse_accounts,
    parse_cash_transactions,
    parse_open_positions,
)
from portfolio_analytics.ingestion.schema import synth_security_id

STATEMENT = """<FlexQueryResponse queryName="All" type="AF">
  <FlexStatements count="1">
    <FlexStatement accountId="U33333333" fromDate="20250606" toDate="20260605">
      <AccountInformation accountId="U33333333" acctAlias="Growth" accountType="Individual" currency="USD"/>
      <EquitySummaryInBase>
        <EquitySummaryByReportDateInBase accountId="U33333333" reportDate="20260604" cash="500" total="9500" currency="BASE"/>
        <EquitySummaryByReportDateInBase accountId="U33333333" reportDate="20260605" cash="600" total="10000" currency="BASE"/>
      </EquitySummaryInBase>
      <OpenPositions>
        <OpenPosition accountId="U33333333" symbol="RKLB" assetCategory="STK" position="100" markPrice="30" positionValue="3000" costBasisPrice="20" costBasisMoney="2000" currency="USD"/>
        <OpenPosition accountId="U33333333" symbol="SPY" assetCategory="ETF" position="10" markPrice="500" positionValue="5000" costBasisPrice="450" costBasisMoney="4500" currency="USD"/>
        <OpenPosition accountId="U33333333" symbol="AAPL  250620C" assetCategory="OPT" position="1" markPrice="5" positionValue="500" costBasisPrice="3" costBasisMoney="300" currency="USD"/>
        <OpenPosition accountId="U33333333" symbol="" assetCategory="CASH" position="600" markPrice="1" currency="USD"/>
      </OpenPositions>
      <CashTransactions>
        <CashTransaction accountId="U33333333" symbol="RKLB" type="Dividends" amount="50" currency="USD" dateTime="20260301"/>
        <CashTransaction accountId="U33333333" symbol="" type="Broker Interest Received" amount="12.34" currency="USD" dateTime="20260401"/>
        <CashTransaction accountId="U33333333" symbol="" type="Broker Interest Paid" amount="-3.00" currency="USD" dateTime="20260401"/>
        <CashTransaction accountId="U33333333" symbol="RKLB" type="Withholding Tax" amount="-7.50" currency="USD" dateTime="20260301"/>
      </CashTransactions>
      <Trades>
        <Trade accountId="U33333333" symbol="MSFT" assetCategory="STK" tradeDate="20260115" dateTime="20260115;153000" quantity="-10" proceeds="2000" cost="-1505" fifoPnlRealized="495" openDateTime="20240601;100000" buySell="SELL" levelOfDetail="CLOSED_LOT"/>
      </Trades>
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>"""


def _connector():
    return IbkrFlexConnector("tok", "qid", fetcher=lambda: STATEMENT)


# ── section parsers ───────────────────────────────────────────────────────────
def test_parse_accounts_latest_report_date_and_info():
    accts = parse_accounts(ET.fromstring(STATEMENT))
    a = accts["U33333333"]
    assert a["nav"] == 10000.0  # latest reportDate (20260605) wins over 20260604
    assert a["cash"] == 600.0
    assert a["name"] == "Growth"
    assert a["account_type"] == "Individual"


def test_parse_open_positions_skips_opt_and_cash():
    holds = parse_open_positions(ET.fromstring(STATEMENT))
    tickers = {sec.ticker for _, sec in holds}
    assert tickers == {"RKLB", "SPY"}  # OPT + CASH skipped
    rklb = next(h for h, _ in holds if h.security_id == synth_security_id("RKLB"))
    assert rklb.quantity == 100.0
    assert rklb.avg_cost == 20.0
    assert rklb.cost_basis == 2000.0
    assert rklb.market_value_local == 3000.0


def test_parse_cash_transactions_type_mapping():
    acts = parse_cash_transactions(ET.fromstring(STATEMENT))
    by_type = {}
    for act, _ in acts:
        by_type.setdefault(act.type, []).append(act.amount)
    assert by_type[TxnType.DIVIDEND] == [50.0]
    assert by_type[TxnType.INTEREST] == [12.34]  # received → income
    # interest paid + withholding → FEE (expenses); amounts are positive magnitudes
    assert sorted(by_type[TxnType.FEE]) == [3.0, 7.5]


def test_parse_open_positions_skips_lot_level_rows():
    # IBKR emits both a LOT row and a SUMMARY rollup when lot detail is on — only
    # the SUMMARY must be kept, else the position is double-counted.
    xml = """<FlexQueryResponse><FlexStatements><FlexStatement accountId="U1">
      <OpenPositions>
        <OpenPosition accountId="U1" symbol="RKLB" assetCategory="STK" levelOfDetail="SUMMARY" position="100" markPrice="30" positionValue="3000" costBasisPrice="20" costBasisMoney="2000" currency="USD"/>
        <OpenPosition accountId="U1" symbol="RKLB" assetCategory="STK" levelOfDetail="LOT" position="60" markPrice="30" positionValue="1800" costBasisPrice="20" costBasisMoney="1200" currency="USD"/>
        <OpenPosition accountId="U1" symbol="RKLB" assetCategory="STK" levelOfDetail="LOT" position="40" markPrice="30" positionValue="1200" costBasisPrice="20" costBasisMoney="800" currency="USD"/>
      </OpenPositions></FlexStatement></FlexStatements></FlexQueryResponse>"""
    holds = parse_open_positions(ET.fromstring(xml))
    assert len(holds) == 1  # LOT rows dropped
    assert holds[0][0].quantity == 100.0


def test_map_cash_type_edge_cases():
    assert _map_cash_type("Broker Interest Paid", -3.0) is TxnType.FEE
    assert _map_cash_type("Broker Interest Received", 3.0) is TxnType.INTEREST
    assert _map_cash_type("Deposits/Withdrawals", 100.0) is TxnType.DEPOSIT
    assert _map_cash_type("Deposits/Withdrawals", -100.0) is TxnType.WITHDRAWAL
    assert _map_cash_type("Some Mystery Type", 1.0) is None


# ── connector ─────────────────────────────────────────────────────────────────
def test_connector_full_snapshot(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # isolate bronze writes
    snap = _connector().sync()
    assert snap.error is None
    assert len(snap.accounts) == 1 and snap.accounts[0].nav_usd == 10000.0
    assert snap.accounts[0].account_id == "U33333333"  # Flex: number is the id
    assert {h.security_id for h in snap.holdings} == {synth_security_id("RKLB"), synth_security_id("SPY")}
    assert len(snap.activities) == 4
    assert len(snap.realized_lots) == 1 and snap.realized_lots[0][1].ticker == "MSFT"
    assert {s.ticker for s in snap.securities} == {"RKLB", "SPY", "MSFT"}


def test_connector_lands_bronze(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _connector().sync()
    assert (tmp_path / "cache" / "connectors" / "bronze" / "ibkr_flex" / "latest.raw").exists()


def test_connector_fail_soft_on_fetch_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def _boom():
        raise RuntimeError("token expired")

    snap = IbkrFlexConnector("t", "q", fetcher=_boom).sync()
    assert snap.error == "token expired"
    assert snap.accounts == []


def test_connector_fail_soft_on_bad_xml(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    snap = IbkrFlexConnector("t", "q", fetcher=lambda: "<not><valid").sync()
    assert snap.error is not None and "Flex XML" in snap.error


def test_connector_satisfies_protocol():
    from portfolio_analytics.ingestion.base import BrokerConnector

    assert isinstance(_connector(), BrokerConnector)
