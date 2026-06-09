"""Tests for the OFX/QFX investment importer (broker_io/ofx_import)."""

from __future__ import annotations

from datetime import date

import pytest

from portfolio_analytics.broker_io.ofx_import import (
    OfxConnector,
    OfxImportError,
    parse_ofx,
)
from portfolio_analytics.domain.ledger import TxnType

_HEADER = """OFXHEADER:100
DATA:OFXSGML
VERSION:102
SECURITY:NONE
ENCODING:USASCII
CHARSET:1252
COMPRESSION:NONE
OLDFILEUID:NONE
NEWFILEUID:NONE

"""

# A complete investment statement: BUY, SELL, DIVIDEND income, a cash deposit, plus a
# SECLIST resolving the CUSIP to a ticker.
_TXNS = """<BUYSTOCK><INVBUY><INVTRAN><FITID>T1<DTTRADE>20240115120000</INVTRAN><SECID><UNIQUEID>037833100<UNIQUEIDTYPE>CUSIP</SECID><UNITS>10<UNITPRICE>150.00<COMMISSION>1.00<TOTAL>-1501.00<SUBACCTSEC>CASH<SUBACCTFUND>CASH</INVBUY><BUYTYPE>BUY</BUYSTOCK>
<SELLSTOCK><INVSELL><INVTRAN><FITID>T2<DTTRADE>20240601120000</INVTRAN><SECID><UNIQUEID>037833100<UNIQUEIDTYPE>CUSIP</SECID><UNITS>-4<UNITPRICE>200.00<COMMISSION>1.00<TOTAL>799.00<SUBACCTSEC>CASH<SUBACCTFUND>CASH</INVSELL><SELLTYPE>SELL</SELLSTOCK>
<INCOME><INVTRAN><FITID>T3<DTTRADE>20240301120000</INVTRAN><SECID><UNIQUEID>037833100<UNIQUEIDTYPE>CUSIP</SECID><INCOMETYPE>DIV<TOTAL>4.40<SUBACCTSEC>CASH<SUBACCTFUND>CASH</INCOME>
<INVBANKTRAN><STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20240602120000<TRNAMT>1000.00<FITID>T4<NAME>ACH DEPOSIT</STMTTRN><SUBACCTFUND>CASH</INVBANKTRAN>"""

_SECLIST = """<SECLISTMSGSRSV1><SECLIST><STOCKINFO><SECINFO><SECID><UNIQUEID>037833100<UNIQUEIDTYPE>CUSIP</SECID><SECNAME>Apple Inc<TICKER>AAPL</SECINFO></STOCKINFO></SECLIST></SECLISTMSGSRSV1>"""


def _ofx(txns: str = _TXNS, *, seclist: str = _SECLIST, acct: str = "U99999999") -> str:
    return (
        _HEADER
        + "<OFX>\n"
        "<SIGNONMSGSRSV1><SONRS><STATUS><CODE>0<SEVERITY>INFO</STATUS>"
        "<DTSERVER>20240802120000<LANGUAGE>ENG</SONRS></SIGNONMSGSRSV1>\n"
        "<INVSTMTMSGSRSV1><INVSTMTTRNRS><TRNUID>1<STATUS><CODE>0<SEVERITY>INFO</STATUS>\n"
        "<INVSTMTRS><DTASOF>20240801120000<CURDEF>USD\n"
        f"<INVACCTFROM><BROKERID>example.com<ACCTID>{acct}</INVACCTFROM>\n"
        "<INVTRANLIST><DTSTART>20240101120000<DTEND>20240801120000\n"
        f"{txns}\n"
        "</INVTRANLIST></INVSTMTRS></INVSTMTTRNRS></INVSTMTMSGSRSV1>\n"
        f"{seclist}\n"
        "</OFX>\n"
    )


class TestHappyPath:
    def test_parses_all_transactions(self):
        r = parse_ofx(_ofx())
        assert r.parsed == 4 and r.skipped == 0 and r.errors == []
        assert [a.type for a in r.snapshot.activities] == [
            TxnType.BUY, TxnType.SELL, TxnType.DIVIDEND, TxnType.DEPOSIT
        ]

    def test_cusip_resolved_to_ticker(self):
        r = parse_ofx(_ofx())
        assert [(s.security_id, s.ticker) for s in r.snapshot.securities] == [("EQ:AAPL:USD", "AAPL")]
        assert r.snapshot.activities[0].security_id == "EQ:AAPL:USD"

    def test_buy_fields(self):
        buy = parse_ofx(_ofx()).snapshot.activities[0]
        assert buy.when == date(2024, 1, 15)
        assert buy.quantity == 10 and buy.price == 150 and buy.fees == 1

    def test_sell_units_magnitude(self):
        # OFX encodes a sale as negative units; canonical carries a positive magnitude.
        sell = parse_ofx(_ofx()).snapshot.activities[1]
        assert sell.type == TxnType.SELL and sell.quantity == 4

    def test_cash_deposit_has_no_security(self):
        deposit = parse_ofx(_ofx()).snapshot.activities[3]
        assert deposit.type == TxnType.DEPOSIT and deposit.security_id == "" and deposit.amount == 1000

    def test_account_and_institution(self):
        acct = parse_ofx(_ofx()).snapshot.accounts[0]
        assert acct.number == "U99999999" and acct.institution == "example.com"

    def test_accepts_bytes_and_str(self):
        assert parse_ofx(_ofx().encode("utf-8")).parsed == parse_ofx(_ofx()).parsed == 4


class TestSecurityFallback:
    def test_missing_seclist_falls_back_to_cusip(self):
        # No SECLIST → the holding is tracked by its raw CUSIP rather than dropped.
        r = parse_ofx(_ofx(seclist=""))
        assert r.parsed == 4
        assert r.snapshot.activities[0].security_id == "EQ:037833100:USD"


class TestIncomeAndCashTypes:
    def test_interest_income(self):
        txn = ("<INCOME><INVTRAN><FITID>I1<DTTRADE>20240301120000</INVTRAN>"
               "<SECID><UNIQUEID>037833100<UNIQUEIDTYPE>CUSIP</SECID>"
               "<INCOMETYPE>INTEREST<TOTAL>2.00<SUBACCTSEC>CASH<SUBACCTFUND>CASH</INCOME>")
        r = parse_ofx(_ofx(txns=txn))
        assert r.snapshot.activities[0].type == TxnType.INTEREST

    def test_bank_debit_is_withdrawal(self):
        txn = ("<INVBANKTRAN><STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20240602120000<TRNAMT>-250.00"
               "<FITID>W1<NAME>ACH WITHDRAWAL</STMTTRN><SUBACCTFUND>CASH</INVBANKTRAN>")
        r = parse_ofx(_ofx(txns=txn, seclist=""))
        act = r.snapshot.activities[0]
        assert act.type == TxnType.WITHDRAWAL and act.amount == 250

    def test_bank_fee_by_trntype(self):
        txn = ("<INVBANKTRAN><STMTTRN><TRNTYPE>FEE<DTPOSTED>20240602120000<TRNAMT>-5.00"
               "<FITID>F1<NAME>ADVISORY FEE</STMTTRN><SUBACCTFUND>CASH</INVBANKTRAN>")
        r = parse_ofx(_ofx(txns=txn, seclist=""))
        assert r.snapshot.activities[0].type == TxnType.FEE


class TestReinvestAndUnsupported:
    def test_reinvested_dividend_is_a_buy(self):
        # A reinvested dividend buys shares → canonical BUY (so the lot/cost basis grows).
        txn = ("<REINVEST><INVTRAN><FITID>R1<DTTRADE>20240315120000</INVTRAN>"
               "<SECID><UNIQUEID>037833100<UNIQUEIDTYPE>CUSIP</SECID>"
               "<INCOMETYPE>DIV<TOTAL>-50.00<SUBACCTSEC>CASH<UNITS>0.3<UNITPRICE>165.00</REINVEST>")
        r = parse_ofx(_ofx(txns=txn))
        act = r.snapshot.activities[0]
        assert act.type == TxnType.BUY and act.quantity == pytest.approx(0.3) and act.price == 165

    def test_unsupported_transaction_is_skipped_not_fatal(self):
        # A TRANSFER isn't modeled → recorded as a skip (with its fitid), not dropped silently.
        txn = ("<TRANSFER><INVTRAN><FITID>X1<DTTRADE>20240401120000</INVTRAN>"
               "<SECID><UNIQUEID>037833100<UNIQUEIDTYPE>CUSIP</SECID>"
               "<SUBACCTSEC>CASH<UNITS>5<TFERACTION>IN<POSTYPE>LONG</TRANSFER>")
        r = parse_ofx(_ofx(txns=txn))
        assert r.parsed == 0 and r.skipped == 1
        assert r.errors[0].ref == "fitid X1" and "TRANSFER" in r.errors[0].reason


class TestStructuralErrors:
    def test_non_ofx_raises(self):
        with pytest.raises(OfxImportError):
            parse_ofx("this is not an OFX file")

    def test_no_investment_statement_raises(self):
        with pytest.raises(OfxImportError):
            parse_ofx("")


class TestConnectorWrapper:
    def test_sync_returns_snapshot(self):
        conn = OfxConnector(_ofx())
        snap = conn.sync()
        assert conn.source == "ofx"
        assert snap.error is None and len(snap.activities) == 4
        assert conn.result is not None and conn.result.parsed == 4

    def test_structural_error_degrades(self):
        snap = OfxConnector("garbage").sync()
        assert snap.error is not None and snap.activities == []
