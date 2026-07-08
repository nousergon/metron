"""Self-hosted analytics event sink — ``POST /track`` + the funnel read (metron-ops#34).

Covers the endpoint (valid event inserts; malformed rejected) and the funnel count query
(distinct sessions per event over a date range). The service-layer edges live in
``test_events_service.py``; this file drives the HTTP surface via the TestClient.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta


def test_track_inserts_and_returns_id(client):
    r = client.post(
        "/track",
        json={"event_name": "demo_viewed", "session_id": "sess-abc", "props": {"ref": "hn"}},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["event_name"] == "demo_viewed"
    # A real UUID is echoed back.
    uuid.UUID(body["id"])


def test_track_accepts_optional_user_id(client):
    uid = str(uuid.uuid4())
    r = client.post(
        "/track",
        json={"event_name": "signup_completed", "session_id": "s1", "user_id": uid},
    )
    assert r.status_code == 201
    # The event is countable in the funnel afterwards.
    rows = client.get("/track/funnel").json()["rows"]
    assert {"event_name": "signup_completed", "sessions": 1} in rows


def test_track_rejects_missing_event_name(client):
    r = client.post("/track", json={"session_id": "s1"})
    assert r.status_code == 422  # pydantic: required field


def test_track_rejects_blank_event_name(client):
    r = client.post("/track", json={"event_name": "   ", "session_id": "s1"})
    assert r.status_code == 422  # min_length → pydantic 422 on the trimmed-empty edge


def test_track_rejects_non_snake_case_event_name(client):
    r = client.post("/track", json={"event_name": "Demo Viewed!", "session_id": "s1"})
    assert r.status_code == 422
    assert "snake_case" in r.json()["detail"]


def test_track_rejects_missing_session_id(client):
    r = client.post("/track", json={"event_name": "demo_viewed"})
    assert r.status_code == 422  # required field


def test_track_rejects_blank_session_id(client):
    r = client.post("/track", json={"event_name": "demo_viewed", "session_id": "  "})
    assert r.status_code == 422


def test_track_rejects_overlong_session_id(client):
    r = client.post("/track", json={"event_name": "demo_viewed", "session_id": "x" * 65})
    assert r.status_code == 422  # exceeds the 64-char column width


class TestFunnel:
    def test_counts_distinct_sessions_per_event(self, client):
        # Two sessions view the demo; one of them also signs up. The demo count is the
        # DISTINCT-session count (2), not the raw event count (3, with the duplicate below).
        for sid in ("a", "b"):
            client.post("/track", json={"event_name": "demo_viewed", "session_id": sid})
        client.post("/track", json={"event_name": "demo_viewed", "session_id": "a"})  # dup session
        client.post("/track", json={"event_name": "signup_submitted", "session_id": "a"})

        rows = client.get("/track/funnel").json()["rows"]
        by_name = {row["event_name"]: row["sessions"] for row in rows}
        assert by_name == {"demo_viewed": 2, "signup_submitted": 1}
        # Ordered by descending session count.
        assert rows[0]["event_name"] == "demo_viewed"

    def test_empty_funnel(self, client):
        out = client.get("/track/funnel").json()
        assert out["rows"] == []

    def test_date_range_filters(self, client):
        client.post("/track", json={"event_name": "demo_viewed", "session_id": "a"})
        # Stored ts is server-side UTC (naive on SQLite); compare against naive UTC bounds
        # so the range filter is apples-to-apples. `params=` lets httpx encode the ISO string.
        an_hour_ago = (datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)).isoformat()
        an_hour_hence = (datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)).isoformat()
        # A window ending before "now" excludes the just-inserted event.
        assert client.get("/track/funnel", params={"end": an_hour_ago}).json()["rows"] == []
        # A window bracketing "now" includes it.
        rows = client.get("/track/funnel", params={"start": an_hour_ago, "end": an_hour_hence}).json()["rows"]
        assert rows == [{"event_name": "demo_viewed", "sessions": 1}]
