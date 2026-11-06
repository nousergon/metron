"""metron-ops-I392: a FAILED benchmark backfill must not suppress retries for the memo window."""

from __future__ import annotations

from datetime import date

import pytest

from api.services import performance as perf


def test_failed_backfill_is_retried_on_the_next_load(monkeypatch):
    calls: list[list[str]] = []

    def flaky_backfill(session, symbols, start, end, source=None):
        calls.append(list(symbols))
        if len(calls) == 1:
            raise RuntimeError("synthetic transient failure")

    monkeypatch.setattr(perf.price_service, "ensure_security", lambda session, sym: None)
    monkeypatch.setattr(perf.price_service, "close_history_by_symbol", lambda session, syms: {})
    monkeypatch.setattr(perf.price_service, "backfill_prices", flaky_backfill)

    start, end = date(2026, 1, 2), date(2026, 1, 30)
    with pytest.raises(RuntimeError):
        perf._ensure_benchmark_coverage(None, ["SYN"], start, end, None)
    perf._ensure_benchmark_coverage(None, ["SYN"], start, end, None)
    assert calls == [["SYN"], ["SYN"]]


def test_completed_backfill_is_still_memoized(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(perf.price_service, "ensure_security", lambda session, sym: None)
    monkeypatch.setattr(perf.price_service, "close_history_by_symbol", lambda session, syms: {})
    monkeypatch.setattr(perf.price_service, "backfill_prices",
                        lambda session, symbols, start, end, source=None: calls.append(list(symbols)))

    start, end = date(2026, 1, 2), date(2026, 1, 30)
    perf._ensure_benchmark_coverage(None, ["SYN"], start, end, None)
    perf._ensure_benchmark_coverage(None, ["SYN"], start, end, None)
    assert calls == [["SYN"]]
