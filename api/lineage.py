"""Response shape for the producer-object lineage of a served market value (metron-ops-I373).

``InputSourceOut`` is the wire form of :class:`portfolio_analytics.prices.source.InputSource`:
which stored object a value was read from and which immutable version of it. Responses carry it
as an additive field, so a served metric can be traced to its producer input.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from portfolio_analytics.prices.source import InputSource


class InputSourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    artifact: str
    version_id: str | None = None
    etag: str | None = None
    adjustment_basis: str | None = None
    ref: str


def lineage(source: InputSource | None) -> InputSourceOut | None:
    return None if source is None else InputSourceOut.model_validate(source)

