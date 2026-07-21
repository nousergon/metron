"""Minimal outbound alerting — Telegram today, the only channel this deploy has
credentials for (``TELEGRAM_BOT_TOKEN``/``TELEGRAM_CHAT_ID`` are SSM-hydrated on
every deploy per ``infrastructure/deploy-on-merge.sh``, but nothing has sent to them
yet — ``api/main.py``'s flow-doctor comment flags "an alert channel is a tracked
follow-up"). This module is that follow-up's first consumer: the nightly
custodian-reconciliation job (metron-ops#216).

Best-effort by construction: a failed or unconfigured send must never fail the job
that's trying to alert — it logs instead (an ERROR log routes through flow-doctor's
S3 capture per ``api/main.py``'s ``setup_logging``, so an alert failure is still
visible, just not paged).
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.parse
import urllib.request

from api.config import settings

logger = logging.getLogger(__name__)

_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def send_telegram_alert(text: str) -> bool:
    """POST ``text`` to the configured Telegram chat. Returns True on a confirmed
    send, False otherwise (unconfigured or the request failed) — never raises."""
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        logger.error("alert (Telegram unconfigured, logging instead): %s", text)
        return False
    url = _TELEGRAM_API.format(token=settings.telegram_bot_token)
    data = urllib.parse.urlencode({"chat_id": settings.telegram_chat_id, "text": text}).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=10) as resp:  # noqa: S310 — fixed Telegram API host
            if resp.status != 200:
                logger.error("Telegram alert failed (HTTP %s): %s", resp.status, text)
                return False
    except (urllib.error.URLError, TimeoutError) as e:
        logger.error("Telegram alert failed (%s): %s", e, text)
        return False
    return True
