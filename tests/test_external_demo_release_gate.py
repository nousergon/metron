"""The unlicensed-display detector (metron-ops-I326, Stage A audit finding).

`EXTERNAL_DEMO_RELEASED` correctly 403s invite creation/redemption and 401s live
sessions while off (metron-ops-I310) — but nothing paged if it were flipped on before
the display licence (metron-ops#24) landed. These tests pin the violation predicate,
the two legitimate silent states, the UNMEASURABLE state for a broken read, and the
non-zero CLI exit that makes the systemd unit red on anything but COMPLIANT.

The predicate reads `display_licence_confirmed`, NOT `feed_entitled`. The first version
of this detector read `feed_entitled` and was structurally unable to fire: that flag
answers "does this deployment offer the feed-dependent wedge", defaults to True on the
owner build, and the external demo is built feed-on by design, so the violation was
unsatisfiable. `test_feed_entitled_alone_can_never_silence_the_violation` is the
regression test for that, and it is the reason this module exists in its current shape.
"""

from __future__ import annotations

from types import SimpleNamespace

from api.services import alerting, external_demo_release_gate
from api.services.external_demo_release_gate import GateState, evaluate


def _settings(released: object = False, licensed: object = False, feed_entitled: object = True):
    return SimpleNamespace(
        external_demo_released=released,
        display_licence_confirmed=licensed,
        feed_entitled=feed_entitled,
    )


def test_unreleased_is_compliant_regardless_of_the_licence():
    assert evaluate(_settings(released=False, licensed=False)).state is GateState.COMPLIANT
    assert evaluate(_settings(released=False, licensed=True)).state is GateState.COMPLIANT


def test_released_without_a_confirmed_licence_is_the_violation():
    """The exact unlicensed-display state: EXTERNAL_DEMO_RELEASED on, licence not confirmed."""
    result = evaluate(_settings(released=True, licensed=False))
    assert result.state is GateState.VIOLATION
    assert result.external_demo_released is True
    assert result.display_licence_confirmed is False


def test_feed_entitled_alone_can_never_silence_the_violation():
    """Regression for the defect this module was corrected for (2026-09-17).

    `feed_entitled` is True on the owner build by default and the external demo is built
    feed-on, so a predicate keyed on it could not fire on the one condition the detector
    exists to catch. Here the deployment is fully feed-entitled and released, with no
    confirmed licence — it must still be a VIOLATION.
    """
    result = evaluate(_settings(released=True, licensed=False, feed_entitled=True))
    assert result.state is GateState.VIOLATION
    assert result.feed_entitled is True  # reported as context, never part of the verdict


def test_released_with_a_confirmed_licence_is_the_post_gate24_state_and_is_silent():
    """The end state once metron-ops#24 lands. It reads an attestation, not a date, so it
    must never page once the licence is actually in place."""
    result = evaluate(_settings(released=True, licensed=True))
    assert result.state is GateState.COMPLIANT


def test_a_confirmed_licence_without_the_feed_is_still_silent():
    """`feed_entitled` is not the subject. A licensed deployment that happens not to offer
    the feed wedge is a product-shape question, not a compliance violation."""
    result = evaluate(_settings(released=True, licensed=True, feed_entitled=False)).state
    assert result is GateState.COMPLIANT


def test_a_non_bool_flag_is_unmeasurable_not_compliant():
    """The dangerous failure mode for any detector: erroring into 'all clear'. A
    renamed attribute or a broken monkeypatch must not free-pass as COMPLIANT."""
    assert evaluate(_settings(released="not-a-bool", licensed=True)).state is GateState.UNMEASURABLE
    assert evaluate(_settings(released=True, licensed="yes")).state is GateState.UNMEASURABLE


def test_a_missing_flag_is_unmeasurable():
    settings_obj = SimpleNamespace(external_demo_released=True)  # licence flag absent
    assert evaluate(settings_obj).state is GateState.UNMEASURABLE


def test_an_unreadable_feed_context_does_not_change_the_verdict():
    """`feed_entitled` is context. Its absence must not turn a decidable check into
    UNMEASURABLE — only the two deciding flags can do that."""
    settings_obj = SimpleNamespace(external_demo_released=True, display_licence_confirmed=False)
    result = evaluate(settings_obj)
    assert result.state is GateState.VIOLATION
    assert result.feed_entitled is None


def test_cli_exits_non_zero_on_violation(monkeypatch):
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(alerting, "send_alert", lambda t, **kw: sent.append((t, kw)) or True)
    monkeypatch.setattr(
        external_demo_release_gate, "evaluate",
        lambda: external_demo_release_gate.GateCheck(GateState.VIOLATION, True, False, True),
    )
    assert external_demo_release_gate.main([]) == 1
    text, kwargs = sent[0]
    assert "EXTERNAL_DEMO_RELEASED" in text and "DISPLAY_LICENCE_CONFIRMED" in text
    assert kwargs["severity"] == "critical"
    assert kwargs["dedup_key"] == "metron-external-demo-release-gate-violation"


def test_cli_exits_non_zero_and_pages_on_unmeasurable(monkeypatch):
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(alerting, "send_alert", lambda t, **kw: sent.append((t, kw)) or True)
    monkeypatch.setattr(
        external_demo_release_gate, "evaluate",
        lambda: external_demo_release_gate.GateCheck(GateState.UNMEASURABLE, None, True, True),
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
        lambda: external_demo_release_gate.GateCheck(GateState.COMPLIANT, False, False, True),
    )
    assert external_demo_release_gate.main([]) == 0
    assert sent == []


def test_evaluate_reads_live_settings_by_default():
    """No settings_obj passed — the default path reads api.config.settings, same as the
    process it runs in. The real settings default to the compliant unreleased state, with
    the licence NOT confirmed: an unset attestation must never read as confirmed."""
    result = evaluate()
    assert result.state is GateState.COMPLIANT
    assert result.external_demo_released is False
    assert result.display_licence_confirmed is False
