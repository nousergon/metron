"""Tests for loaders/ibkr_flex.py — IBKR Flex Query fetch, parse, and union cache."""

from __future__ import annotations

from datetime import date

import pytest

from portfolio_analytics.broker_io import flex_xml as ibkr_flex
from portfolio_analytics.broker_io.flex_xml import (
    IbkrFlexError,
    _load_cached_lots,
    _parse_flex_date,
    _save_lots,
    _union,
    fetch_flex_xml,
    get_realized_lots,
    load_flex_files,
    parse_realized_lots,
)

# A FlexQueryResponse with: a long-term gain, a short-term loss, and a CASH row
# that must be skipped.
STATEMENT_XML = """<FlexQueryResponse queryName="Realized" type="AF">
  <FlexStatements count="1">
    <FlexStatement accountId="U11111111" fromDate="20260101" toDate="20260601">
      <Trades>
        <Trade accountId="U11111111" symbol="AAPL" assetCategory="STK"
               tradeDate="20260115" dateTime="20260115;153000" quantity="-10"
               proceeds="2000" cost="-1505" fifoPnlRealized="495"
               openDateTime="20240601;100000" buySell="SELL" levelOfDetail="CLOSED_LOT"/>
        <Trade accountId="U11111111" symbol="TSLA" assetCategory="STK"
               tradeDate="20260220" dateTime="20260220;100000" quantity="-5"
               proceeds="1000" cost="-1200" fifoPnlRealized="-200"
               openDateTime="20251201;100000" buySell="SELL" levelOfDetail="CLOSED_LOT"/>
        <Trade accountId="U11111111" symbol="FDRXX" assetCategory="CASH"
               tradeDate="20260101" dateTime="20260101" quantity="-100"
               proceeds="100" cost="-100" fifoPnlRealized="0"
               openDateTime="20260101" levelOfDetail="CLOSED_LOT"/>
      </Trades>
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>"""

SEND_OK = (
    "<FlexStatementResponse><Status>Success</Status>"
    "<ReferenceCode>987654</ReferenceCode>"
    "<Url>https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement</Url>"
    "</FlexStatementResponse>"
)
SEND_FAIL = (
    "<FlexStatementResponse><Status>Fail</Status>"
    "<ErrorCode>1012</ErrorCode><ErrorMessage>Token has expired.</ErrorMessage>"
    "</FlexStatementResponse>"
)
GENERATING = (
    "<FlexStatementResponse><Status>Warn</Status>"
    "<ErrorCode>1019</ErrorCode><ErrorMessage>Statement generation in progress.</ErrorMessage>"
    "</FlexStatementResponse>"
)


class _FakeResponse:
    def __init__(self, body: str):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self) -> bytes:
        return self._body.encode("utf-8")


def _opener_from(send: str, statements: list[str]):
    """Build an injectable opener: SendRequest URLs get ``send``; GetStatement URLs
    return the next item from ``statements`` in order."""
    box = {"i": 0}

    def opener(url: str, timeout: int = 30):
        if "SendRequest" in url:
            return _FakeResponse(send)
        body = statements[min(box["i"], len(statements) - 1)]
        box["i"] += 1
        return _FakeResponse(body)

    return opener


# ── date parsing ─────────────────────────────────────────────────────────────


def test_parse_flex_date_formats():
    assert _parse_flex_date("20260115") == date(2026, 1, 15)
    assert _parse_flex_date("20260115;153000") == date(2026, 1, 15)
    assert _parse_flex_date("2026-01-15") == date(2026, 1, 15)
    assert _parse_flex_date("") is None
    assert _parse_flex_date(None) is None
    assert _parse_flex_date("garbage") is None


# ── parsing ──────────────────────────────────────────────────────────────────


# The real schema closed-lot detail emits <Lot> rows with BLANK proceeds + populated
# cost (verified against a live IBKR statement, 2026-06-08).
LOT_XML = """<FlexQueryResponse><FlexStatements><FlexStatement accountId="U22222222">
  <Trades>
    <Lot accountId="U22222222" symbol="RIO" assetCategory="STK" tradeDate="20260510"
         quantity="44" cost="6614.80" proceeds="" fifoPnlRealized="1342.83"
         openDateTime="20260201;202600" holdingPeriodDateTime="20260201;202600"/>
    <Lot accountId="U22222222" symbol="META" assetCategory="STK" tradeDate="20260122"
         quantity="2" cost="845.82" proceeds="" fifoPnlRealized="414.18"
         openDateTime="20240425"/>
  </Trades></FlexStatement></FlexStatements></FlexQueryResponse>"""


def test_parse_lot_elements_with_blank_proceeds():
    """<Lot> rows leave proceeds blank — derive it from cost so gain == fifoPnlRealized."""
    lots = parse_realized_lots(LOT_XML)
    assert len(lots) == 2
    by = {rg.ticker: (acct, rg) for acct, rg in lots}

    acct, rio = by["RIO"]
    assert acct == "U22222222"
    assert rio.cost_basis == pytest.approx(6614.80)
    assert rio.gain == pytest.approx(1342.83)  # == IBKR fifoPnlRealized
    assert rio.proceeds == pytest.approx(6614.80 + 1342.83)
    assert rio.long_term is False  # ~98 days

    _, meta = by["META"]
    assert meta.gain == pytest.approx(414.18)
    assert meta.long_term is True  # open 2024-04-25 → close 2026-01-22


def test_parse_realized_lots_closed_lots():
    lots = parse_realized_lots(STATEMENT_XML)
    assert len(lots) == 2  # CASH row skipped
    by_ticker = {rg.ticker: (acct, rg) for acct, rg in lots}

    acct, aapl = by_ticker["AAPL"]
    assert acct == "U11111111"
    assert aapl.open_date == date(2024, 6, 1)
    assert aapl.close_date == date(2026, 1, 15)
    assert aapl.gain == pytest.approx(495.0)  # proceeds 2000 − basis 1505
    assert aapl.long_term is True  # held > 1 year

    _, tsla = by_ticker["TSLA"]
    assert tsla.gain == pytest.approx(-200.0)
    assert tsla.long_term is False  # ~81 days


def test_parse_realized_lots_falls_back_when_no_closed_lot_level():
    # No levelOfDetail="CLOSED_LOT" — fall back to rows with fifoPnlRealized + openDateTime.
    xml = """<FlexQueryResponse><FlexStatements><FlexStatement accountId="U1">
      <Trades>
        <Trade accountId="U1" symbol="MSFT" assetCategory="STK" tradeDate="20260301"
               quantity="-3" proceeds="900" fifoPnlRealized="100" openDateTime="20250101"/>
      </Trades></FlexStatement></FlexStatements></FlexQueryResponse>"""
    lots = parse_realized_lots(xml)
    assert len(lots) == 1
    assert lots[0][1].gain == pytest.approx(100.0)


def test_parse_realized_lots_skips_rows_missing_dates():
    xml = """<FlexQueryResponse><FlexStatements><FlexStatement accountId="U1"><Trades>
      <Trade accountId="U1" symbol="X" assetCategory="STK" fifoPnlRealized="5"
             openDateTime="" dateTime="" levelOfDetail="CLOSED_LOT"/>
    </Trades></FlexStatement></FlexStatements></FlexQueryResponse>"""
    assert parse_realized_lots(xml) == []


def test_parse_realized_lots_empty_statement():
    xml = '<FlexQueryResponse><FlexStatements count="0"></FlexStatements></FlexQueryResponse>'
    assert parse_realized_lots(xml) == []


# ── fetch (Flex Web Service) ─────────────────────────────────────────────────


def test_fetch_flex_xml_happy_path():
    opener = _opener_from(SEND_OK, [STATEMENT_XML])
    out = fetch_flex_xml("tok", "qid", opener=opener)
    assert "FlexQueryResponse" in out


def test_fetch_flex_xml_polls_until_ready():
    opener = _opener_from(SEND_OK, [GENERATING, GENERATING, STATEMENT_XML])
    sleeps: list[float] = []
    out = fetch_flex_xml("tok", "qid", opener=opener, sleep=sleeps.append)
    assert "FlexQueryResponse" in out
    assert sleeps  # it waited between polls


def test_fetch_flex_xml_send_failure_raises():
    opener = _opener_from(SEND_FAIL, [STATEMENT_XML])
    with pytest.raises(IbkrFlexError, match="1012"):
        fetch_flex_xml("tok", "qid", opener=opener)


def test_fetch_flex_xml_times_out_when_never_ready():
    opener = _opener_from(SEND_OK, [GENERATING])
    with pytest.raises(IbkrFlexError, match="not ready"):
        fetch_flex_xml("tok", "qid", poll_attempts=2, opener=opener, sleep=lambda *_: None)


def test_fetch_flex_xml_get_statement_error_raises():
    err = "<FlexStatementResponse><Status>Fail</Status><ErrorCode>1020</ErrorCode></FlexStatementResponse>"
    opener = _opener_from(SEND_OK, [err])
    with pytest.raises(IbkrFlexError, match="1020"):
        fetch_flex_xml("tok", "qid", opener=opener, sleep=lambda *_: None)


# ── file ingest ──────────────────────────────────────────────────────────────


def test_load_flex_files_parses_and_skips_bad(tmp_path):
    (tmp_path / "good.xml").write_text(STATEMENT_XML)
    (tmp_path / "bad.xml").write_text("<not valid")
    lots = load_flex_files(tmp_path)
    assert len(lots) == 2  # good parsed, bad skipped


def test_load_flex_files_missing_dir():
    assert load_flex_files(ibkr_flex.FLEX_FILE_DIR / "nope-missing") == []


# ── union cache ──────────────────────────────────────────────────────────────


def test_union_dedupes_by_lot_key():
    a = parse_realized_lots(STATEMENT_XML)
    merged = _union(a, list(a))  # same lots twice
    assert len(merged) == 2


def test_save_and_load_cached_lots_round_trip(tmp_path):
    store = tmp_path / "lots.json"
    lots = parse_realized_lots(STATEMENT_XML)
    _save_lots(lots, store)
    loaded = _load_cached_lots(store)
    assert {rg.ticker for _, rg in loaded} == {"AAPL", "TSLA"}
    assert sorted(rg.gain for _, rg in loaded) == pytest.approx([-200.0, 495.0])


def test_load_cached_lots_missing_and_corrupt(tmp_path):
    assert _load_cached_lots(tmp_path / "absent.json") == []
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert _load_cached_lots(bad) == []


def test_load_cached_lots_skips_malformed_rows(tmp_path):
    store = tmp_path / "lots.json"
    store.write_text(
        '[{"ticker": "X"}, {"account_id":"U1","ticker":"Y","open_date":"2025-01-01",'
        '"close_date":"2026-02-01","quantity":1,"proceeds":10,"cost_basis":5}]'
    )
    loaded = _load_cached_lots(store)
    assert [rg.ticker for _, rg in loaded] == ["Y"]  # malformed first row dropped


# ── entry point ──────────────────────────────────────────────────────────────


def test_get_realized_lots_file_only_no_token(tmp_path):
    file_dir = tmp_path / "drop"
    file_dir.mkdir()
    (file_dir / "stmt.xml").write_text(STATEMENT_XML)
    cache = tmp_path / "lots.json"
    lots, error = get_realized_lots(None, None, file_dir=file_dir, cache_path=cache)
    assert error is None
    assert len(lots) == 2
    assert cache.exists()  # accumulated to cache


def test_get_realized_lots_fetch_merges_and_persists(tmp_path):
    cache = tmp_path / "lots.json"
    opener = _opener_from(SEND_OK, [STATEMENT_XML])
    lots, error = get_realized_lots("tok", "qid", file_dir=tmp_path / "none", cache_path=cache, opener=opener)
    assert error is None
    assert len(lots) == 2


def test_get_realized_lots_fetch_error_keeps_cache(tmp_path):
    cache = tmp_path / "lots.json"
    _save_lots(parse_realized_lots(STATEMENT_XML), cache)  # pre-existing cache
    opener = _opener_from(SEND_FAIL, [STATEMENT_XML])
    lots, error = get_realized_lots("tok", "qid", file_dir=tmp_path / "none", cache_path=cache, opener=opener)
    assert error is not None and "1012" in error  # surfaced, not swallowed
    assert len(lots) == 2  # cached lots still returned
