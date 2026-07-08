"""Event sink service layer — ``record_event`` + ``funnel_counts`` (metron-ops#34).

Unit-level coverage of the validation branches and the funnel query semantics, driven off
the raw ``db_session`` fixture (no HTTP). The endpoint wiring is in ``test_track_api.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from api.db import models
from api.services import events


def test_record_event_persists_row(db_session):
    ev = events.record_event(db_session, event_name="demo_viewed", session_id="s1", props={"ref": "hn"})
    assert isinstance(ev.id, uuid.UUID)
    stored = db_session.get(models.Event, ev.id)
    assert stored.event_name == "demo_viewed"
    assert stored.session_id == "s1"
    assert stored.user_id is None
    assert stored.props == {"ref": "hn"}


def test_record_event_defaults_props_to_empty_dict(db_session):
    ev = events.record_event(db_session, event_name="demo_viewed", session_id="s1")
    assert ev.props == {}


def test_record_event_attaches_user_id(db_session):
    uid = uuid.uuid4()
    ev = events.record_event(db_session, event_name="signup_completed", session_id="s1", user_id=uid)
    assert ev.user_id == uid


@pytest.mark.parametrize("bad", ["", "   "])
def test_record_event_rejects_blank_name(db_session, bad):
    with pytest.raises(ValueError, match="event_name is required"):
        events.record_event(db_session, event_name=bad, session_id="s1")


def test_record_event_rejects_overlong_name(db_session):
    with pytest.raises(ValueError, match="exceeds"):
        events.record_event(db_session, event_name="a" * 81, session_id="s1")


@pytest.mark.parametrize("bad", ["Demo", "demo viewed", "demo-viewed", "1demo", "demo!"])
def test_record_event_rejects_non_snake_case_name(db_session, bad):
    with pytest.raises(ValueError, match="snake_case"):
        events.record_event(db_session, event_name=bad, session_id="s1")


@pytest.mark.parametrize("bad", ["", "   "])
def test_record_event_rejects_blank_session_id(db_session, bad):
    with pytest.raises(ValueError, match="session_id is required"):
        events.record_event(db_session, event_name="demo_viewed", session_id=bad)


def test_record_event_rejects_overlong_session_id(db_session):
    with pytest.raises(ValueError, match="session_id exceeds"):
        events.record_event(db_session, event_name="demo_viewed", session_id="x" * 65)


def test_record_event_rejects_non_dict_props(db_session):
    with pytest.raises(ValueError, match="props must be an object"):
        events.record_event(db_session, event_name="demo_viewed", session_id="s1", props=["not", "a", "dict"])


def test_funnel_counts_distinct_sessions(db_session):
    for sid in ("a", "b"):
        events.record_event(db_session, event_name="demo_viewed", session_id=sid)
    events.record_event(db_session, event_name="demo_viewed", session_id="a")  # dup session
    events.record_event(db_session, event_name="signup_submitted", session_id="a")

    counts = {c.event_name: c.sessions for c in events.funnel_counts(db_session)}
    assert counts == {"demo_viewed": 2, "signup_submitted": 1}


def test_funnel_counts_empty(db_session):
    assert events.funnel_counts(db_session) == []


def test_funnel_counts_ordered_by_sessions_desc(db_session):
    events.record_event(db_session, event_name="rare", session_id="a")
    for sid in ("a", "b", "c"):
        events.record_event(db_session, event_name="common", session_id=sid)
    result = events.funnel_counts(db_session)
    assert [c.event_name for c in result] == ["common", "rare"]


def test_funnel_counts_window_bounds(db_session):
    events.record_event(db_session, event_name="demo_viewed", session_id="a")
    # Stored ts is server-side UTC, naive on SQLite — compare against naive UTC bounds.
    now = datetime.now(UTC).replace(tzinfo=None)
    # end-before-now excludes it; start-in-past includes it.
    assert events.funnel_counts(db_session, end=now - timedelta(hours=1)) == []
    included = events.funnel_counts(db_session, start=now - timedelta(hours=1))
    assert [(c.event_name, c.sessions) for c in included] == [("demo_viewed", 1)]
