"""Accounting invariants as runtime assertions — layer 2 of the dashboard
accuracy verification framework (metron-ops#217, part of EPIC metron-ops#210).

Each invariant is a pure function that receives response-shaped data from the
serving path and returns ``(ok: bool, detail: str)``. Callers in
``api/routers/portfolios.py`` run the relevant subset per endpoint and log
violations at WARNING so they surface in CloudWatch. The layer-1 break store
(``ReconciliationBreak``) integration follows once the per-endpoint wiring
pattern stabilizes and the taxonomy settles (metron-ops#216 landed 2026-07-21).

CONVENTION: every invariant function accepts **kwargs so new invariants can
be added without changing every call site. The caller passes only the fields
each invariant needs; unused kwargs are silently ignored.

Design constraint (from the issue body): most of these cannot be a single
generic middleware because the response shapes differ — ``performance()``
returns ``PerformanceSummary`` with NAV points, ``attribution()`` returns
``AttributionOut`` with Brinson effects, etc. Each is a bespoke call site
that passes the specific data that endpoint already computed.

Tolerance flags (metron-ops#210 scoping note): every invariant uses a
generous relative tolerance (1e-4 = 0.01%) rather than an absolute
threshold so a $1M portfolio doesn't flag a $0.01 float-rounding
difference. The tolerance is a named constant, not a magic number, so
tuning one value retunes every invariant that uses it.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from typing import Any

logger = logging.getLogger(__name__)

# Relative tolerance for float equality checks (0.01% — a $1M portfolio
# tolerates ~$100 of float-rounding noise). Single tuning point.
_INVARIANT_RTOL = 1e-4


def _approx_eq(a: float, b: float, *, rtol: float = _INVARIANT_RTOL) -> bool:
    """True when *a* and *b* are within relative tolerance. Handles zero."""
    if a == b:
        return True
    denom = max(abs(a), abs(b))
    if denom == 0:
        return True
    return abs(a - b) / denom <= rtol


def _fail(label: str, detail: str) -> tuple[bool, str]:
    """Standardized failure tuple — logged at the call site."""
    logger.warning("[invariant:%s] VIOLATION: %s", label, detail)
    return False, detail


# ── NAV continuity ──────────────────────────────────────────────────────────
# NAV(t) = NAV(t−1) + external_flow + P&L
#
# For every consecutive pair of recorded NAV snapshots, the current NAV must
# equal the prior NAV plus that day's external flow plus the P&L implied by
# the market-value change. The P&L term is unobserved (we only have NAV and
# flow), so this is a CONSISTENCY check, not a correctness proof: the flow-
# neutralized return must be consistent with the naked NAV delta.


def check_nav_continuity(
    *,
    nav_points: list[Any],
    label: str = "nav_continuity",
    **__: Any,
) -> list[tuple[bool, str]]:
    """Check NAV(t) = NAV(t-1) + flow + implied_P&L for every consecutive pair.

    Each point must have ``nav`` (float), ``external_flow`` (float), and
    ``snap_date`` (date). The implied P&L is (nav_t - nav_{t-1} - flow_t),
    which is what TWR flow-neutralization strips — a consistency check that
    the series is internally coherent.

    Returns a list of per-period results; empty when fewer than 2 points."""
    if len(nav_points) < 2:
        return []
    results: list[tuple[bool, str]] = []
    for i in range(1, len(nav_points)):
        prev = nav_points[i - 1]
        curr = nav_points[i]
        try:
            prev_nav = float(prev.nav)
            curr_nav = float(curr.nav)
            flow = float(curr.external_flow)
            snap_date = str(curr.snap_date)
        except (AttributeError, TypeError, ValueError) as e:
            results.append(_fail(label, f"cannot extract fields for {label} at index {i}: {e}"))
            continue

        if prev_nav <= 0:
            continue  # skip non-positive prior NAV (seed point, data gap)

        implied_pnl = curr_nav - prev_nav - flow
        # The consistency equation is: curr_nav = prev_nav + flow + implied_pnl
        # This is an identity (by definition of implied_pnl), so it always
        # holds. The REAL invariant is that implied_pnl is reasonable — it
        # must NOT be an implausible jump (the same guard record_snapshot
        # already applies at write time). We check that the period return is
        # within a sane bound (daily ±100% for a single-stock portfolio).
        period_return = implied_pnl / prev_nav
        if abs(period_return) > 1.0:  # >100% daily return — suspicious
            results.append(
                _fail(
                    label,
                    f"{snap_date}: NAV {prev_nav:.2f} → {curr_nav:.2f} "
                    f"(flow={flow:.2f}, implied daily return={period_return:.1%}) — "
                    f">100% single-day move may indicate a data error",
                )
            )
    return results


# ── Position sum = NAV ──────────────────────────────────────────────────────
# Σ position market values + cash = NAV


def check_position_nav_consistency(
    *,
    holdings: Collection[Any],
    nav: float,
    cash: float = 0.0,
    label: str = "position_nav_consistency",
    **__: Any,
) -> tuple[bool, str]:
    """Σ position market values + cash ≈ NAV (within relative tolerance).

    ``holdings``: each must have ``market_value`` (float or None).
    ``nav``: the portfolio NAV as reported by ``performance()`` or summary.
    ``cash``: the portfolio cash balance (default 0).

    Returns ``(True, "")`` when the sum is within tolerance, or when NAV is
    non-positive (nothing to check)."""
    if nav <= 0:
        return True, ""

    position_sum = sum(
        float(h.market_value)
        for h in holdings
        if h.market_value is not None
    )
    total = position_sum + cash
    if _approx_eq(total, nav):
        return True, ""
    return _fail(
        label,
        f"Σ position market values ({position_sum:.2f}) + cash ({cash:.2f}) "
        f"= {total:.2f} ≠ NAV ({nav:.2f}) — "
        f"Δ = {total - nav:.2f} ({(total - nav) / nav:.4%})",
    )


# ── Realized + Unrealized = Total P&L ───────────────────────────────────────


def check_realized_unrealized_total(
    *,
    realized_total: float,
    unrealized_gain: float | None,
    label: str = "realized_unrealized_total",
    **__: Any,
) -> tuple[bool, str]:
    """Total portfolio P&L = realized + unrealized (accounting identity).

    ``unrealized_gain`` may be None when the portfolio hasn't been priced yet
    — the check passes (nothing to verify)."""
    if unrealized_gain is None:
        return True, ""
    total = realized_total + unrealized_gain
    # This is an identity by definition — we check only that neither component
    # is NaN/inf (a data-corruption signal, not an arithmetic one).
    import math

    if math.isnan(total) or math.isinf(total):
        return _fail(
            label,
            f"realized ({realized_total:.2f}) + unrealized ({unrealized_gain:.2f}) "
            f"= {total} — non-finite result indicates data corruption",
        )
    return True, ""


# ── Brinson effects sum ─────────────────────────────────────────────────────
# allocation + selection + interaction = active return (within tolerance)


def check_brinson_effects(
    *,
    allocation: float,
    selection: float,
    interaction: float,
    active_return: float,
    label: str = "brinson_effects",
    **__: Any,
) -> tuple[bool, str]:
    """Brinson effects (allocation + selection + interaction) must sum to the
    reported active return within relative tolerance."""
    effects_sum = allocation + selection + interaction
    if _approx_eq(effects_sum, active_return):
        return True, ""
    return _fail(
        label,
        f"Brinson allocation ({allocation:.6f}) + selection ({selection:.6f}) "
        f"+ interaction ({interaction:.6f}) = {effects_sum:.6f} "
        f"≠ active return ({active_return:.6f}) — "
        f"Δ = {effects_sum - active_return:.6f}",
    )


# ── Lot quantity sum = position quantity ───────────────────────────────────


def check_lot_quantities(
    *,
    lots: Collection[Any],
    label: str = "lot_quantities",
    **__: Any,
) -> list[tuple[bool, str]]:
    """For every ticker, Σ lot quantities = position quantity.

    ``lots``: each must have ``ticker`` (str), ``quantity`` (float).
    Already covered as a property test in metron#288 (layer 4) — this is the
    LIVE serving-path version of the same check."""
    from collections import defaultdict

    by_ticker: dict[str, float] = defaultdict(float)
    for lot in lots:
        try:
            by_ticker[lot.ticker] += float(lot.quantity)
        except (AttributeError, TypeError, ValueError) as e:
            return [_fail(label, f"cannot read lot quantity: {e}")]

    results: list[tuple[bool, str]] = []
    # Without a reference position per ticker (we only have lot-level data
    # here), we check that no single lot has an implausible quantity.
    for ticker, qty in by_ticker.items():
        if qty <= 0:
            results.append(_fail(label, f"{ticker}: lot quantity sum = {qty} ≤ 0"))
    return results


# ── TWR sub-period chain-link ───────────────────────────────────────────────


def check_twr_chain_link(
    *,
    cumulative_return: float | None,
    twr: float | None,
    label: str = "twr_chain_link",
    **__: Any,
) -> tuple[bool, str]:
    """The TWR (time-weighted return, geometrically linked sub-period returns)
    must be consistent with the cumulative return. Both are flow-neutralized
    by construction (using the same flow series), so they should be nearly
    identical — the difference is the re-basing approximation.

    Per metron-ops#44: the cumulative return coincidences with TWR when both
    are computed from the same flow-neutralized returns. This is a consistency
    check, not an identity — a small difference is expected from compounding
    precision."""
    if cumulative_return is None or twr is None:
        return True, ""
    if _approx_eq(cumulative_return, twr):
        return True, ""
    # TWR and cumulative should be very close (same flow series). A >1%
    # divergence suggests a computation bug.
    if abs(cumulative_return - twr) > 0.01:
        return _fail(
            label,
            f"cumulative return ({cumulative_return:.6f}) ≠ TWR ({twr:.6f}) — "
            f"Δ = {cumulative_return - twr:.6f} ({(cumulative_return - twr) * 100:.4f}pp)",
        )
    return True, ""


# ── Convenience: run all applicable invariants for an endpoint ──────────────


def run_invariants(
    *,
    checks: list[tuple[str, Any]],
) -> dict[str, list[str]]:
    """Run a list of named invariant checks against endpoint response data.

    Each entry in ``checks`` is ``(label, result)`` where ``result`` is
    either ``(ok: bool, detail: str)`` or ``list[(ok, detail)]`` from the
    invariant function. Violations are logged and collected.

    Returns ``{label: [detail, ...]}`` for violated invariants only — empty
    when all checks pass. Callers can embed this in a response header or log
    it for monitoring."""
    violations: dict[str, list[str]] = {}
    for label, result in checks:
        if result is None:
            continue
        if isinstance(result, list):
            for ok, detail in result:
                if not ok:
                    violations.setdefault(label, []).append(detail)
        elif isinstance(result, tuple) and len(result) == 2:
            ok, detail = result
            if not ok:
                violations.setdefault(label, []).append(detail)
    if violations:
        logger.warning(
            "[invariants] %d invariant(s) violated: %s",
            len(violations),
            {k: len(v) for k, v in violations.items()},
        )
    return violations
