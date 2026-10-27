"""Regression for CodeQL py/log-injection alerts #67/#68 (alpha-engine-config-I11665).

The index-contributions source logged the request-path ``index`` and the derived
S3 key raw, so a CR/LF in either could forge a separate log line. Every
externally sourced value now goes through ``log_safe`` at the log call.
"""

from __future__ import annotations

import logging
from datetime import date

from portfolio_analytics.index_contributions import source as ic_source
from portfolio_analytics.index_contributions import spine_source
from portfolio_analytics.log_safe import log_safe

FORGED = "SPX\r\nCRITICAL forged line\x1b[2J"


def test_log_safe_escapes_crlf_and_control_characters():
    out = log_safe(FORGED)
    assert "\r" not in out and "\n" not in out and "\x1b" not in out
    assert out == "SPX\\r\\nCRITICAL forged line\\x1b[2J"


def test_log_safe_passes_ordinary_text_through_and_truncates_long_values():
    assert log_safe("market_data/index_contributions/SPX/2026-10-26.json") == (
        "market_data/index_contributions/SPX/2026-10-26.json"
    )
    assert log_safe(ValueError("bad value")) == "bad value"
    long = log_safe("x" * 2000)
    assert long.endswith("...[truncated]") and len(long) < 600


def _single_line_records(caplog) -> list[str]:
    msgs = [r.getMessage() for r in caplog.records]
    assert msgs, "expected a warning to be logged"
    for m in msgs:
        assert "\n" not in m and "\r" not in m and "\x1b" not in m
    return msgs


def test_date_mismatch_warning_cannot_be_forged_by_the_index(caplog):
    raw = {
        "schema_version": 1,
        "index": "SPX",
        "proxy_symbol": "SPY",
        "trading_day": "2026-10-23",
        "prior_close_date": "2026-10-22",
        "index_return_pct": 0.5,
        "constituents": [],
    }
    with caplog.at_level(logging.WARNING, logger=ic_source.logger.name):
        got = ic_source.fetch_index_contributions(FORGED, date(2026, 10, 26), source=lambda i, d: raw)
    assert got is None
    (msg,) = _single_line_records(caplog)
    assert "SPX\\r\\nCRITICAL forged line" in msg


def test_malformed_artifact_warning_is_single_line(caplog):
    raw = {"index": FORGED}  # missing required fields -> KeyError path
    with caplog.at_level(logging.WARNING, logger=ic_source.logger.name):
        got = ic_source.fetch_index_contributions("SPX", date(2026, 10, 26), source=lambda i, d: raw)
    assert got is None
    _single_line_records(caplog)


class _FailingS3:
    def get_object(self, **kwargs):
        raise RuntimeError("NoSuchKey\nERROR forged line")


def test_spine_read_failure_warning_is_single_line(caplog):
    with caplog.at_level(logging.WARNING, logger=spine_source.logger.name):
        got = spine_source.spine_index_contributions(FORGED, date(2026, 10, 26), s3=_FailingS3())
    assert got is None
    msgs = _single_line_records(caplog)
    assert len(msgs) == 2  # dated key, then latest.json fallback
    assert all("forged line" in m for m in msgs)
