"""Detect the unlicensed-display state: EXTERNAL_DEMO_RELEASED on without a confirmed
display licence (metron-ops-I326, Stage A audit finding).

**Why this exists.** `EXTERNAL_DEMO_RELEASED` (default False, `api/config.py`) is the
only control standing between the feed-on external demo (metron-ops#310) and displaying
licensed market data to people outside the account. It correctly gates invite creation,
redemption, and live sessions — see `api/services/external_demo.py`. What was missing is
any signal if it is switched on: no alarm, no console row, nothing that would notice the
product serving licensed data before the display licence (metron-ops#24) closes.
Principle 7: a control whose violation nothing reports is unobserved, not safe.

**The observable.** `settings.display_licence_confirmed` (`api/config.py`), which is
True only once metron-ops#24 records the display licence purchased and confirmed in
writing. The violation is exactly `external_demo_released AND NOT
display_licence_confirmed`: the demo is serving external viewers over a deployment with
no display rights. `external_demo_released AND display_licence_confirmed` is the intended
post-#24 end state and must stay silent — this reads an attestation, never a date or a
milestone name, so it cannot itself become a reason to keep the flag off after #24 lands.

**Why not `feed_entitled`** (measured 2026-09-17, and the reason this module was
corrected before metron-ops-I326 was closed): the first version of this gate read
`settings.feed_entitled`, on the belief that it turns True when a licensed feed is
provisioned. It does not. Its own definition in `api/config.py` is "does this deployment
OFFER the feed-dependent wedge", it defaults to **True** on the owner build, and the
external demo is built with the feed ON by design (plan §5.1 A6). So the violation
predicate was unsatisfiable by construction: on the live box the detector reported
`compliant: external_demo_released=False feed_entitled=True`, and flipping the release
flag would have kept it compliant — a detector structurally blind to the single condition
it exists to catch. `feed_entitled` is still reported alongside the verdict as context; it
is never what decides it.

**Never green on missing data.** If either flag cannot be read as a bool off the live
`Settings` object — an attribute renamed, a monkeypatch that leaves a non-bool, a future
refactor — this reports UNMEASURABLE, not COMPLIANT. A detector that free-passes on a
broken read is the same defect class the deploy-drift check exists to avoid (see
`api/services/deploy_drift.py`): detect the missing effect, never assume compliance from
a missing signal.

Mirrors `api/services/deploy_drift.py`'s shape: a state-reading `evaluate`/`check` pair,
a `report()` that pages through `api.services.alerting.send_alert`, and a `main()` CLI
entry point a systemd timer in `metron-ops` drives directly (no `api.maintenance`
import — see that module's docstring for why a maintenance-CLI dependency broke the
drift check's DB-free guarantee; the same reasoning applies here, this check does not
touch the database either).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum

logger = logging.getLogger(__name__)


class GateState(StrEnum):
    """Three states, not two — see the module docstring's "never green on missing data"."""

    COMPLIANT = "compliant"        # released=False, or released=True AND licence confirmed
    VIOLATION = "violation"        # released=True AND licence NOT confirmed — unlicensed display
    UNMEASURABLE = "unmeasurable"  # the flags could not be read as booleans off settings


@dataclass(frozen=True)
class GateCheck:
    state: GateState
    external_demo_released: bool | None
    display_licence_confirmed: bool | None
    # Context only — reported beside the verdict, never part of it. See the module
    # docstring's "Why not `feed_entitled`".
    feed_entitled: bool | None = None


def evaluate(settings_obj=None) -> GateCheck:
    """Read the two flags and classify. Never raises — a broken read is UNMEASURABLE,
    not an exception, because this runs from a systemd oneshot where a raise and a
    'nothing to page' free-pass look identical to the operator unless the state is
    named. `report()` still pages on UNMEASURABLE (see below), so nothing is lost."""
    if settings_obj is None:
        from api.config import settings as settings_obj

    released = getattr(settings_obj, "external_demo_released", None)
    licensed = getattr(settings_obj, "display_licence_confirmed", None)
    feed = getattr(settings_obj, "feed_entitled", None)
    released = released if isinstance(released, bool) else None
    licensed = licensed if isinstance(licensed, bool) else None
    feed = feed if isinstance(feed, bool) else None

    if released is None or licensed is None:
        return GateCheck(GateState.UNMEASURABLE, released, licensed, feed)
    if released and not licensed:
        return GateCheck(GateState.VIOLATION, released, licensed, feed)
    return GateCheck(GateState.COMPLIANT, released, licensed, feed)


def check() -> GateCheck:
    result = evaluate()
    if result.state is GateState.VIOLATION:
        logger.error(
            "external-demo release gate VIOLATION: external_demo_released=%s "
            "display_licence_confirmed=%s (feed_entitled=%s, context only) — market data "
            "may be reaching external viewers without the display licence (metron-ops#24)",
            result.external_demo_released, result.display_licence_confirmed,
            result.feed_entitled,
        )
    elif result.state is GateState.UNMEASURABLE:
        logger.error(
            "external-demo release gate UNMEASURABLE: external_demo_released=%r "
            "display_licence_confirmed=%r did not read as booleans off settings — "
            "compliance cannot be verified",
            result.external_demo_released, result.display_licence_confirmed,
        )
    else:
        logger.info(
            "external-demo release gate compliant: external_demo_released=%s "
            "display_licence_confirmed=%s (feed_entitled=%s, context only)",
            result.external_demo_released, result.display_licence_confirmed,
            result.feed_entitled,
        )
    return result


def report() -> GateCheck:
    """Check and page the operator on VIOLATION or UNMEASURABLE. Returns the check.

    Deduped: VIOLATION is a live compliance exposure and pages on a short window so a
    flip is caught within roughly one detection cycle even after the first page.
    UNMEASURABLE dedupes on the longer window shared with deploy-drift — it means the
    check itself needs attention, not that data is actively leaking.
    """
    from api.services.alerting import send_alert

    result = check()
    if result.state is GateState.VIOLATION:
        send_alert(
            "Metron: EXTERNAL_DEMO_RELEASED is true while DISPLAY_LICENCE_CONFIRMED is "
            "false — market data may be reaching external demo viewers without the "
            "display licence (metron-ops#24). Set EXTERNAL_DEMO_RELEASED=false "
            "immediately, or record the confirmed licence on metron-ops#24 and set "
            "DISPLAY_LICENCE_CONFIRMED=true.",
            severity="critical",
            dedup_key="metron-external-demo-release-gate-violation",
            dedup_window_min=60,
        )
    elif result.state is GateState.UNMEASURABLE:
        send_alert(
            "Metron: the external-demo release gate could not read "
            "external_demo_released/display_licence_confirmed off settings as booleans — "
            "it cannot verify whether the demo is compliant with the display-licence gate "
            "(metron-ops#24, metron-ops-I326).",
            severity="error",
            dedup_key="metron-external-demo-release-gate-unmeasurable",
            dedup_window_min=360,
        )
    return result


def main(argv: list[str] | None = None) -> int:
    """`python -m api.services.external_demo_release_gate` — the systemd unit's entry
    point. Exits non-zero on anything but COMPLIANT, so the unit itself goes red on
    both a real violation and a broken read — never silently 0 on either."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m api.services.external_demo_release_gate",
        description="Page when EXTERNAL_DEMO_RELEASED is on without a confirmed display licence.",
    )
    parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    result = report()
    return 0 if result.state is GateState.COMPLIANT else 1


if __name__ == "__main__":
    raise SystemExit(main())
