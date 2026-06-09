"""Meta endpoints — prove the shared engine is wired in and advertise capabilities."""

from __future__ import annotations

import importlib.metadata

import portfolio_analytics
from fastapi import APIRouter

router = APIRouter(prefix="/meta", tags=["system"])


def _engine_version() -> str:
    try:
        return importlib.metadata.version("portfolio-analytics")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover - editable-install edge
        return getattr(portfolio_analytics, "__version__", "unknown")


@router.get("")
def meta() -> dict:
    """Report the engine version and the descriptive analytics this product offers.

    The trust posture is part of the contract: no AI, no ads/trackers, no advice.
    """
    return {
        "engine": "portfolio-analytics",
        "engine_version": _engine_version(),
        "capabilities": [
            "performance",      # TWR + MWR/XIRR
            "attribution",      # contribution + Brinson
            "risk",             # factor exposures, tracking error, look-through
            "scenarios",        # historical replays, factor shocks, VaR/CVaR
            "income",           # dividends, projected income, yield-on-cost
            "tax",              # realized/unrealized lots, ST/LT, loss-harvest info
        ],
        "posture": {
            "ai": False,
            "ads_or_trackers": False,
            "advice": False,
            "read_only": True,
        },
    }
