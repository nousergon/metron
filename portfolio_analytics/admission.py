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

logger = logging.getLogger(__name__)

__all__ = ["Invalid", "NumberReading", "admit_schema", "read_number"]


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


def read_number(value: object) -> NumberReading:
    """A finite ``float``, ``None`` when absent, or :class:`Invalid` when present but bad.

    Numbers and numeric strings are accepted when finite; ``NaN``, ``±Infinity``,
    booleans and unparseable strings are :class:`Invalid`. JSON ``null`` is
    absent (``None``), never invalid.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return Invalid(value)
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return Invalid(value)
    if not math.isfinite(number):
        return Invalid(value)
    return number
