"""The unlicensed-display detector (metron-ops-I326, Stage A audit finding).

`EXTERNAL_DEMO_RELEASED` correctly 403s invite creation/redemption and 401s live
sessions while off (metron-ops-I310) — but nothing paged if it were flipped on before
the display licence (metron-ops#24) landed. These tests pin the violation predicate,
the two legitimate silent states, the UNMEASURABLE state for a broken read, and the
non-zero CLI exit that makes the systemd unit red on anything but COMPLIANT.
"""

from __future__ import annotations

from types import SimpleNamespace

from api.services import alerting, external_demo_release_gate
from api.services.external_demo_release_gate import GateState, evaluate


def _settings(released: object = False, feed_entitled: object = True):
    return SimpleNamespace(
        external_demo_released=released, feed_entitled=feed_entitled
    )


def test_unreleased_is_compliant_regardless_of_feed():
    assert evaluate(_settings(released=False, feed_entitled=False)).state is GateState.COMPLIANT
    assert evaluate(_settings(released=False, feed_entitled=True)).state is GateState.COMPLIANT


def test_released_with_no_feed_entitlement_is_the_violation():
    """The exact unlicensed-display state: EXTERNAL_DEMO_RELEASED on, feed_entitled off."""
    result = evaluate(_settings(released=True, feed_entitled=False))
    assert result.state is GateState.VIOLATION


def test_released_and_feed_entitled_is_the_intended_post_gate24_state_and_is_silent():
    """This is the end state once metron-ops#24 lands. It reads the entitlement, not a
    date, so it must never page once the licence is actually in place."""
    result = evaluate(_settings(released=True, feed_entitled=True))
    assert result.state is GateState.COMPLIANT


def test_a_non_bool_flag_is_unmeasurable_not_compliant():
    """The dangerous failure mode for any detector: erroring into 'all clear'. A
    renamed attribute or a broken monkeypatch must not free-pass as COMPLIANT."""
    result = evaluate(_settings(released="not-a-bool", feed_entitled=True))
    assert result.state is GateState.UNMEASURABLE


def test_a_missing_flag_is_unmeasurable():
    settings_obj = SimpleNamespace(external_demo_released=True)  # feed_entitled absent
    result = evaluate(settings_obj)
    assert result.state is GateState.UNMEASURABLE


def test_cli_exits_non_zero_on_violation(monkeypatch):
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(alerting, "send_alert", lambda t, **kw: sent.append((t, kw)) or True)
    monkeypatch.setattr(
        external_demo_release_gate, "evaluate",
        lambda: external_demo_release_gate.GateCheck(GateState.VIOLATION, True, False),
    )
    assert external_demo_release_gate.main([]) == 1
    text, kwargs = sent[0]
    assert "EXTERNAL_DEMO_RELEASED" in text and "feed_entitled" in text
    assert kwargs["severity"] == "critical"
    assert kwargs["dedup_key"] == "metron-external-demo-release-gate-violation"


def test_cli_exits_non_zero_and_pages_on_unmeasurable(monkeypatch):
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(alerting, "send_alert", lambda t, **kw: sent.append((t, kw)) or True)
    monkeypatch.setattr(
        external_demo_release_gate, "evaluate",
        lambda: external_demo_release_gate.GateCheck(GateState.UNMEASURABLE, None, True),
    )
    assert external_demo_release_gate.main([]) == 1
    text, kwargs = sent[0]
    assert "cannot verify" in text.lower() or "could not read" in text.lower()
    assert kwargs["severity"] == "error"
    assert kwargs["dedup_key"] == "metron-external-demo-release-gate-unmeasurable"


def test_cli_exits_zero_and_stays_silent_when_compliant(monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(alerting, "send_alert", lambda t, **kw: sent.append(t) or True)
    monkeypatch.setattr(
        external_demo_release_gate, "evaluate",
        lambda: external_demo_release_gate.GateCheck(GateState.COMPLIANT, False, True),
    )
    assert external_demo_release_gate.main([]) == 0
    assert sent == []


def test_evaluate_reads_live_settings_by_default():
    """No settings_obj passed — the default path reads api.config.settings, same as the
    process it runs in. The real settings default to the compliant unreleased state."""
    result = evaluate()
    assert result.state is GateState.COMPLIANT
    assert result.external_demo_released is False
