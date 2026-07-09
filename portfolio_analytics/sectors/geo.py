"""US-vs-international geo classification — the pure bucket rule shared by the
API layer and the diagnostics engine.

Lifted from ``api/services/countries.py`` (metron-ops-I167) so the pure analytics
layer (``portfolio_analytics.domain.diagnostics``) can bucket a holding's country of
domicile without importing the API/DB stack; the countries service re-exports these
names, so its existing consumers are unaffected.

No fabrication: an unresolved country (``None``) lands in its own ``"Unclassified"``
coverage bucket — never guessed into US or International.
"""

from __future__ import annotations

# yfinance returns the US under this exact Title-Case label in ``Ticker.info['country']``.
US_COUNTRY = "United States"

# Canonical sentinel for a multi-country / ex-US holding whose single domicile doesn't
# describe its geographic exposure — a broad-international fund/ETF (e.g. FTIHX, which
# yfinance reports as domiciled "United States" but is ~100% ex-US). Not a yfinance value:
# it only ever reaches ``country`` via a tenant-scoped classification override, so the
# canonical domicile on the global ``securities`` row is never overwritten. It buckets as
# International (it isn't the US), but naming it keeps the override + UI option intentional
# rather than relying on the stringly-typed "anything-not-US" fallthrough.
INTERNATIONAL = "International"

# The three geo buckets, in display order. Unclassified is a COVERAGE bucket, not a guess.
GEO_BUCKETS: tuple[str, ...] = ("US", "International", "Unclassified")


def is_us_domicile(country: str | None) -> bool:
    """Whether ``country`` is the United States (the US side of the US-vs-international
    split). Unclassified (``None``) is NOT US — it lands in its own coverage bucket."""
    return country == US_COUNTRY


def geo_bucket(country: str | None) -> str:
    """The US-vs-international bucket for a holding: ``"US"``, ``"International"``, or
    ``"Unclassified"`` (no country resolved). A specific foreign domicile and the explicit
    ``INTERNATIONAL`` sentinel both bucket as International; the sentinel lets a multi-country
    fund be reclassified out of its (misleading) listing domicile via an override."""
    if country is None:
        return "Unclassified"
    return "US" if is_us_domicile(country) else "International"
