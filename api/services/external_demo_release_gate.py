"""Detect the unlicensed-display state: EXTERNAL_DEMO_RELEASED on without a licensed
feed entitlement (metron-ops-I326, Stage A audit finding).

**Why this exists.** `EXTERNAL_DEMO_RELEASED` (default False, `api/config.py`) is the
only control standing between the feed-on external demo (metron-ops#310) and displaying
licensed market data to people outside the account. It correctly gates invite creation,
redemption, and live sessions — see `api/services/external_demo.py`. What was missing is
any signal if it is switched on: no alarm, no console row, nothing that would notice the
product serving licensed data before the display licence (metron-ops#24) closes.
Principle 7: a control whose violation nothing reports is unobserved, not safe.

**The observable.** `settings.feed_entitled` is the licensed-feed-entitlement axis
already wired through `/meta/entitlements` and `/meta/status` (metron-ops#43) — it is
False on the no-feed multi-tenant beta and True once a licensed feed is provisioned
behind the `CloseSource`/`IntradaySource`/`FundamentalsSource` seams (metron-ops#24). The
violation is exactly `external_demo_released AND NOT feed_entitled`: the demo is serving
external viewers over a deployment that has not been provisioned with display rights.
`external_demo_released AND feed_entitled` is the intended post-#24 end state and must
stay silent — this reads the entitlement, never a date or a milestone name, so it cannot
itself become a reason to keep the flag off after #24 lands.

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

    COMPLIANT = "compliant"        # released=False, or released=True AND feed_entitled=True
    VIOLATION = "violation"        # released=True AND feed_entitled=False — unlicensed display
    UNMEASURABLE = "unmeasurable"  # the flags could not be read as booleans off settings


@dataclass(frozen=True)
class GateCheck:
    state: GateState
    external_demo_released: bool | None
    feed_entitled: bool | None


def evaluate(settings_obj=None) -> GateCheck:
    """Read the two flags and classify. Never raises — a broken read is UNMEASURABLE,
    not an exception, because this runs from a systemd oneshot where a raise and a
    'nothing to page' free-pass look identical to the operator unless the state is
    named. `report()` still pages on UNMEASURABLE (see below), so nothing is lost."""
    if settings_obj is None:
        from api.config import settings as settings_obj

    released = getattr(settings_obj, "external_demo_released", None)
    feed = getattr(settings_obj, "feed_entitled", None)
    released = released if isinstance(released, bool) else None
    feed = feed if isinstance(feed, bool) else None

    if released is None or feed is None:
        return GateCheck(GateState.UNMEASURABLE, released, feed)
    if released and not feed:
        return GateCheck(GateState.VIOLATION, released, feed)
    return GateCheck(GateState.COMPLIANT, released, feed)


def check() -> GateCheck:
    result = evaluate()
    if result.state is GateState.VIOLATION:
        logger.error(
            "external-demo release gate VIOLATION: external_demo_released=%s "
            "feed_entitled=%s — licensed market data may be reaching external viewers "
            "without the display entitlement (metron-ops#24)",
            result.external_demo_released, result.feed_entitled,
        )
    elif result.state is GateState.UNMEASURABLE:
        logger.error(
            "external-demo release gate UNMEASURABLE: external_demo_released=%r "
            "feed_entitled=%r did not read as booleans off settings — compliance cannot "
            "be verified",
            result.external_demo_released, result.feed_entitled,
        )
    else:
        logger.info(
            "external-demo release gate compliant: external_demo_released=%s "
            "feed_entitled=%s",
            result.external_demo_released, result.feed_entitled,
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
            "Metron: EXTERNAL_DEMO_RELEASED is true while feed_entitled is false — "
            "licensed market data may be reaching external demo viewers without the "
            "display entitlement (metron-ops#24). Set EXTERNAL_DEMO_RELEASED=false "
            "immediately, or confirm the licensed feed entitlement is in place.",
            severity="critical",
            dedup_key="metron-external-demo-release-gate-violation",
            dedup_window_min=60,
        )
    elif result.state is GateState.UNMEASURABLE:
        send_alert(
            "Metron: the external-demo release gate could not read "
            "external_demo_released/feed_entitled off settings as booleans — it cannot "
            "verify whether the demo is compliant with the display-licence gate "
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
        description="Page when EXTERNAL_DEMO_RELEASED is on without a licensed feed entitlement.",
    )
    parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    result = report()
    return 0 if result.state is GateState.COMPLIANT else 1


if __name__ == "__main__":
    raise SystemExit(main())
