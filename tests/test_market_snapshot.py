"""Shared intraday-snapshot TTL cache (metron-ops#149 item 3).

``market_snapshot.read_cached_snapshot`` collapses a page's many snapshot reads — the
Holdings live overlay (``intraday.py``) AND the Markets strip (``indices.py``) — into one S3
GetObject per TTL window across BOTH consumers. Freshness is still judged per-call in each
consumer against live ``now``, so a cached-but-aged artifact still reports stale (covered by
the consumer suites). These tests exercise the cache mechanics directly on the shared module.
"""

from __future__ import annotations

import pytest

from api.services import indices, intraday, market_snapshot

_AS_OF = "2026-06-12T15:00:00Z"


def _art(quotes: dict) -> dict:
    return {"schema_version": 1, "as_of_utc": _AS_OF, "source": "yfinance_delayed", "quotes": quotes}


class TestSnapshotCache:
    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch):
        # Fully reset the module cache + a controllable monotonic clock for each test.
        self.clock = [1000.0]
        monkeypatch.setattr(market_snapshot.time, "monotonic", lambda: self.clock[0])
        monkeypatch.setattr(market_snapshot, "_snapshot_cache", None, raising=False)
        monkeypatch.setattr(market_snapshot, "_snapshot_fetched_monotonic", 0.0, raising=False)

    def test_reads_once_within_ttl(self, monkeypatch):
        calls = {"n": 0}

        def _reader():
            calls["n"] += 1
            return _art({"AAPL": {"last": 130.0}})

        monkeypatch.setattr(market_snapshot, "_read_snapshot_s3", _reader)
        first = market_snapshot.read_cached_snapshot()
        for _ in range(5):
            assert market_snapshot.read_cached_snapshot() == first
        assert calls["n"] == 1  # one S3 read serves the whole fan-out

    def test_refetches_after_ttl(self, monkeypatch):
        calls = {"n": 0}
        monkeypatch.setattr(
            market_snapshot, "_read_snapshot_s3", lambda: calls.__setitem__("n", calls["n"] + 1) or _art({})
        )
        market_snapshot.read_cached_snapshot()
        self.clock[0] += market_snapshot._SNAPSHOT_TTL_S + 1  # advance past the window
        market_snapshot.read_cached_snapshot()
        assert calls["n"] == 2

    def test_failed_read_cached_for_window(self, monkeypatch):
        """A transient S3 failure within a page load isn't retried; it degrades to EOD (None)
        for the window, then recovers after the TTL."""
        calls = {"n": 0}
        monkeypatch.setattr(
            market_snapshot, "_read_snapshot_s3", lambda: calls.__setitem__("n", calls["n"] + 1) or None
        )
        assert market_snapshot.read_cached_snapshot() is None
        assert market_snapshot.read_cached_snapshot() is None
        assert calls["n"] == 1

    def test_both_consumers_share_one_read(self, monkeypatch):
        """The Holdings overlay and the Markets strip resolve their default reader through the
        SAME cache — so a render that touches both issues one S3 read per TTL window, not two."""
        calls = {"n": 0}

        def _reader():
            calls["n"] += 1
            return _art({"AAPL": {"last": 130.0}})

        monkeypatch.setattr(market_snapshot, "_read_snapshot_s3", _reader)
        a = intraday._default_reader()
        b = indices._default_reader()
        assert a == b == _art({"AAPL": {"last": 130.0}})
        assert calls["n"] == 1  # one artifact snapshot served both consumers
