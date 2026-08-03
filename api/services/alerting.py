"""Outbound operator alerting — delegates to the fleet alert bus (``krepis.alerts``).

**Why not a private Telegram POST any more.** This module used to hand-roll a
``sendMessage`` call against ``TELEGRAM_BOT_TOKEN``/``TELEGRAM_CHAT_ID``, hydrated by
``infrastructure/deploy-on-merge.sh`` from ``/metron/telegram_bot_token`` and
``/metron/telegram_chat_id``. **Those two SSM parameters have never existed**, so every
alert this module ever produced took the unconfigured branch and degraded to an ERROR
log. Measured 2026-08-03: nine days of nightly SnapTrade 401s (metron-ops#260) were
detected by ``reconciliation`` and alerted on, and not one of them left the box.

``krepis.alerts.publish`` is the fleet's alert path and metron already depends on
krepis: it fans out to the ``alpha-engine-alerts`` SNS topic *and* Telegram, resolving
the bot token from ``/alpha-engine/TELEGRAM_BOT_TOKEN`` — a parameter that exists, that
the dashbox instance role can already read (``alpha-engine-ssm-read``), and that is
rotated in one place for the whole fleet. Verified end-to-end from this deploy's venv
on 2026-08-03: both channels returned ok.

**Severity is the push switch,** not a formatting detail: ``error``/``critical`` buzz
the phone, everything else lands silently in-channel. Pick the tier that matches what
the operator must do about it.

Best-effort by construction: a failed or unconfigured send must never fail the job
that's trying to alert — it logs instead (an ERROR log routes through flow-doctor's
S3 capture per ``api/main.py``'s ``setup_logging``, so an alert failure is still
visible, just not paged).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_SOURCE = "metron"


def send_alert(
    text: str,
    *,
    severity: str = "error",
    dedup_key: str | None = None,
    dedup_window_min: int | None = None,
) -> bool:
    """Publish ``text`` to the fleet alert channels. Returns True when at least one
    channel delivered it (or a dedup marker proves an equivalent alert already
    reached the operator), False otherwise — never raises.

    ``severity`` follows the fleet contract: ``error``/``critical`` push, everything
    else is silent in-channel. ``dedup_key`` suppresses repeats of the same condition
    within ``dedup_window_min`` (fleet default when unset) — pass one for anything a
    scheduled job re-detects on every run, so a persistent fault pages once rather than
    once per timer fire.
    """
    try:
        from krepis import alerts

        kwargs: dict = {"severity": severity, "source": _SOURCE, "dedup_key": dedup_key}
        if dedup_window_min is not None:
            kwargs["dedup_window_min"] = dedup_window_min
        result = alerts.publish(text, **kwargs)
    except Exception as e:  # noqa: BLE001 — an alerting failure must never fail its caller
        logger.error("alert publish failed (%s), logging instead: %s", e, text)
        return False
    if not result.any_ok:
        logger.error(
            "alert undelivered (sns=%s telegram=%s), logging instead: %s",
            result.sns.detail, result.telegram.detail, text,
        )
        return False
    return True
