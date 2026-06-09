"""Tests for the header-flexible CSV transaction importer (broker_io/csv_import)."""

from __future__ import annotations

from datetime import date

import pytest

from portfolio_analytics.broker_io.csv_import import (
    CsvConnector,
    CsvImportError,
    parse_transactions_csv,
)
from portfolio_analytics.domain.ledger import TxnType

CANONICAL = """date,type,symbol,quantity,price,amount,fees,currency
2024-01-15,BUY,AAPL,10,150,1500,1,USD
2024-03-01,DIVIDEND,AAPL,,,4.40,,USD
2024-06-01,SELL,AAPL,5,180,900,1,USD
2024-06-02,DEPOSIT,,,,1000,,USD
"""


class TestHappyPath:
    def test_parses_all_rows(self):
        result = parse_transactions_csv(CANONICAL)
        assert result.parsed == 4
        assert result.skipped == 0
        assert result.errors == []
        acts = result.snapshot.activities
        assert [a.type for a in acts] == [TxnType.BUY, TxnType.DIVIDEND, TxnType.SELL, TxnType.DEPOSIT]

    def test_buy_fields_mapped(self):
        buy = parse_transactions_csv(CANONICAL).snapshot.activities[0]
        assert buy.when == date(2024, 1, 15)
        assert buy.quantity == 10 and buy.price == 150 and buy.fees == 1
        assert buy.security_id == "EQ:AAPL:USD"

    def test_security_master_deduped(self):
        secs = parse_transactions_csv(CANONICAL).snapshot.securities
        assert [s.security_id for s in secs] == ["EQ:AAPL:USD"]  # one master, three referencing rows

    def test_cash_event_has_no_security(self):
        deposit = parse_transactions_csv(CANONICAL).snapshot.activities[3]
        assert deposit.type == TxnType.DEPOSIT
        assert deposit.security_id == ""
        assert deposit.amount == 1000

    def test_default_account_applied(self):
        accts = parse_transactions_csv(CANONICAL).snapshot.accounts
        assert [a.number for a in accts] == ["CSV"]


class TestHeaderFlexibility:
    def test_alias_headers(self):
        csv = "Trade Date,Action,Ticker,Shares,Price,Commission\n2024-01-15,Bought,MSFT,3,400,1\n"
        result = parse_transactions_csv(csv)
        assert result.parsed == 1
        act = result.snapshot.activities[0]
        assert act.type == TxnType.BUY and act.security_id == "EQ:MSFT:USD"
        assert act.quantity == 3 and act.fees == 1

    def test_type_synonyms(self):
        csv = "date,type,symbol,amount\n2024-01-01,Reinvestment,VTI,100\n2024-01-02,contribution,,500\n"
        acts = parse_transactions_csv(csv).snapshot.activities
        assert [a.type for a in acts] == [TxnType.BUY, TxnType.DEPOSIT]

    def test_multiple_accounts(self):
        csv = "date,type,symbol,quantity,price,account\n2024-01-01,BUY,AAPL,1,100,Roth\n2024-01-01,BUY,AAPL,1,100,Taxable\n"
        accts = {a.number for a in parse_transactions_csv(csv).snapshot.accounts}
        assert accts == {"Roth", "Taxable"}


class TestNumberAndDateParsing:
    def test_money_formats(self):
        csv = 'date,type,symbol,quantity,price,amount\n01/15/2024,BUY,AAPL,"1,000","$1,234.56","(500)"\n'
        act = parse_transactions_csv(csv).snapshot.activities[0]
        assert act.quantity == 1000 and act.price == 1234.56
        assert act.amount == 500  # magnitude — sign comes from the type, not the cell

    def test_iso_datetime_date(self):
        csv = "date,type,symbol,quantity,price\n2024-01-15T00:00:00Z,BUY,AAPL,1,100\n"
        assert parse_transactions_csv(csv).snapshot.activities[0].when == date(2024, 1, 15)


class TestBadRows:
    def test_unknown_type_is_skipped_not_fatal(self):
        csv = "date,type,symbol,quantity,price\n2024-01-15,BUY,AAPL,1,100\n2024-01-16,FROBNICATE,AAPL,1,100\n"
        result = parse_transactions_csv(csv)
        assert result.parsed == 1 and result.skipped == 1
        assert result.errors[0].ref == "line 3"  # header is line 1, first data row line 2
        assert "frobnicate" in result.errors[0].reason.lower()

    def test_buy_without_symbol_is_skipped(self):
        csv = "date,type,symbol,quantity,price\n2024-01-15,BUY,,1,100\n"
        result = parse_transactions_csv(csv)
        assert result.parsed == 0 and result.skipped == 1
        assert "symbol" in result.errors[0].reason.lower()

    def test_bad_date_is_skipped(self):
        csv = "date,type,symbol,quantity,price\nnotadate,BUY,AAPL,1,100\n"
        assert parse_transactions_csv(csv).skipped == 1

    def test_one_bad_row_does_not_reject_file(self):
        csv = "date,type,symbol,quantity,price\n2024-01-15,BUY,AAPL,1,100\nbad,BUY,AAPL,1,100\n2024-01-17,BUY,AAPL,1,100\n"
        result = parse_transactions_csv(csv)
        assert result.parsed == 2 and result.skipped == 1


class TestStructuralErrors:
    def test_missing_type_column_raises(self):
        with pytest.raises(CsvImportError, match="type"):
            parse_transactions_csv("date,symbol,quantity\n2024-01-15,AAPL,1\n")

    def test_missing_date_column_raises(self):
        with pytest.raises(CsvImportError, match="date"):
            parse_transactions_csv("type,symbol,quantity\nBUY,AAPL,1\n")

    def test_empty_file_raises(self):
        with pytest.raises(CsvImportError):
            parse_transactions_csv("")


class TestConnectorWrapper:
    """CsvConnector lets a parsed CSV flow through ingest() like any broker source —
    its sync() does no I/O and never raises (a structural error degrades to an empty
    snapshot with .error set, matching the BrokerConnector contract)."""

    def test_sync_returns_snapshot_and_holds_result(self):
        conn = CsvConnector(CANONICAL)
        snap = conn.sync()
        assert conn.source == "csv"
        assert snap.error is None
        assert len(snap.activities) == 4
        assert conn.result is not None and conn.result.parsed == 4

    def test_structural_error_degrades_to_error_snapshot(self):
        snap = CsvConnector("symbol,quantity\nAAPL,1\n").sync()  # no date/type columns
        assert snap.error is not None
        assert snap.activities == []
