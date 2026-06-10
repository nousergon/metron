"""Macro market-context endpoint.

Global (not tenant-scoped) market data read from FRED — kept in its own router since
it isn't portfolio-scoped. The Macro page lives under a portfolio for nav consistency
but the data is the same for everyone.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from api.services import macro

router = APIRouter(tags=["macro"])


class MacroPointOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    obs_date: date
    value: float


class MacroIndicatorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    key: str
    label: str
    units: str
    latest_value: float
    latest_date: date
    prior_value: float | None
    change: float | None
    history: list[MacroPointOut]


class MacroOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    available: bool
    reason: str | None
    as_of: date | None
    indicators: list[MacroIndicatorOut]


@router.get("/macro", response_model=MacroOut)
def get_macro() -> macro.MacroSummary:
    """Latest macro indicators (fed funds, rates, curve, inflation, VIX) from FRED.
    Marked unavailable WITH a reason when no FRED key is configured."""
    return macro.macro_snapshot()
