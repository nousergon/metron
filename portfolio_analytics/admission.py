"""Runtime admission for data-spine artifacts (metron-ops-I373).

Every market-data product Metron reads carries a ``schema_version`` that the
producer pins and that ``tests/contracts/*.schema.json`` mirrors. Until this
module existed those pins were enforced only by CI fixtures: the runtime
readers never looked at the version, and a numeric field converted with a
bare ``float(...)`` admitted ``NaN`` and ``Infinity``. An artifact a newer or
broken producer wrote was therefore read as if it were the shape the reader
was written for, and a non-finite value became a plausible-looking metric.

The rule this module enforces at the reader boundary:

* **An artifact whose ``schema_version`` is not the one the reader was written
  for is refused whole.** Refused means treated exactly like an absent
  artifact (the readers are fail-soft: absent → omitted, never fabricated),
  plus an ERROR log naming the artifact, the version found and the version
  expected — so the refusal reaches the alerting path instead of rendering as
  a quiet gap. A missing version is refused too: an unversioned payload is not
  a contract.
* **A numeric field must be a finite number.** ``NaN``/``±Infinity``, a
  boolean or an unparseable string is *invalid*, which is distinct from
  *absent* (``None``/missing key). Readers record invalid fields so callers
  can tell "the producer had no value" from "the producer sent garbage".

Kept in the engine layer (no ``api`` imports) so both the ``api`` services and
``portfolio_analytics.prices`` share one implementation.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

logger = logging.getLogger(__name__)

__all__ = [
    "CONTENT_MAX_AGE_DAYS",
    "Invalid",
    "NumberReading",
    "admit_schema",
    "content_staleness",
    "finite_field",
    "read_number",
]

#: A daily-cadence artifact whose content ``as_of`` is older than this many calendar days
#: is stale. Five covers a Friday print read on the Wednesday after a Monday holiday.
CONTENT_MAX_AGE_DAYS = 5


def admit_schema(art: dict | None, *, expected: int, artifact: str) -> str | None:
    """``None`` when ``art`` declares exactly ``expected``; otherwise the refusal reason.

    ``art`` being ``None`` (the artifact is absent or unreadable) is not a
    refusal — it is the absent case the caller already handles — so it
    returns ``None`` without logging. Any present artifact that is not a JSON
    object, or whose version is missing, not an integer, or not ``expected``,
    is refused and logged at ERROR.
    """
    if art is None:
        return None
    if not isinstance(art, dict):
        reason = f"{artifact}: artifact is a {type(art).__name__}, not a JSON object"
    else:
        found = art.get("schema_version")
        # bool is an int subclass; `True` is not schema version 1.
        if isinstance(found, bool) or not isinstance(found, int):
            reason = (
                f"{artifact}: schema_version {found!r} is not an integer; this reader is "
                f"pinned to schema_version {expected}"
            )
        elif found != expected:
            reason = (
                f"{artifact}: schema_version {found} is not the pinned {expected}; refusing "
                "the artifact rather than reading an unknown shape as the known one"
            )
        else:
            return None
    logger.error("data-spine artifact refused — %s", reason)
    return reason


@dataclass(frozen=True)
class Invalid:
    """A present value that is not a finite number. Carries what was found."""

    raw: object


NumberReading = float | None | Invalid


def read_number(value: object, *, lo: float | None = None, hi: float | None = None) -> NumberReading:
    """A finite ``float``, ``None`` when absent, or :class:`Invalid` when present but bad.

    ``lo``/``hi`` are the inclusive range the field's declared unit allows (a fraction in
    ``[0, 1]``, an RSI in ``[0, 100]``). A finite value outside it is a value in the wrong
    unit or scale (a percent where a fraction is declared) and is :class:`Invalid` too.

    The pinned schemas type these fields as JSON numbers, so only ``int``/``float``
    are accepted, and only when finite. Strings (numeric or not), booleans, ``NaN``
    and ``±Infinity`` are :class:`Invalid`. JSON ``null`` is absent (``None``),
    never invalid.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return Invalid(value)
    try:
        number = float(value)
    except OverflowError:
        return Invalid(value)
    if not math.isfinite(number):
        return Invalid(value)
    if (lo is not None and number < lo) or (hi is not None and number > hi):
        return Invalid(value)
    return number


def finite_field(d: dict, key: str, invalid: list[str], *, name: str | None = None, lo: float | None = None, hi: float | None = None) -> float | None:
    """``d[key]`` as a finite float, or ``None``.

    Absent (missing key or JSON ``null``) reads as ``None``. Present but not a finite
    number also reads as ``None``, and ``name`` (default ``key``) is appended to
    ``invalid`` so the reader can say the producer sent garbage rather than nothing.
    """
    reading = read_number(d.get(key), lo=lo, hi=hi)
    if isinstance(reading, Invalid):
        invalid.append(name or key)
        return None
    return reading


def content_staleness(
    as_of: date | None,
    *,
    now: datetime,
    written_at: datetime | None = None,
    max_age_days: int = CONTENT_MAX_AGE_DAYS,
    artifact: str = "artifact",
) -> str | None:
    """``None`` when the content's own ``as_of`` is current; otherwise the reason it is not.

    The age is judged on the date the PRODUCER declared for the content, never on when the
    object was written: a producer that re-uploads yesterday's (or last month's) payload
    gives the key a fresh write time and old numbers. ``written_at`` (the store's
    ``LastModified``, when known) is only named in the reason, so the report says "written
    recently, content old" instead of letting the write time pass for freshness.
    A payload that declares no usable ``as_of`` is stale by this rule: its age is unknown.
    """
    if as_of is None:
        return f"{artifact}: declares no usable as_of, so the age of its content is unknown"
    age = (now.astimezone(UTC).date() - as_of).days
    if age <= max_age_days:
        return None
    reason = f"{artifact}: content as_of {as_of.isoformat()} is {age} days old (limit {max_age_days})"
    if written_at is not None:
        recent = now - written_at <= timedelta(days=max_age_days)
        reason += (
            f"; the object was written {written_at.isoformat()}"
            + (" — a recent write of old content" if recent else "")
        )
    return reason
