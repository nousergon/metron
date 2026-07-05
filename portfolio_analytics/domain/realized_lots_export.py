"""Realized-lots export conforming to the telos ``realized_lots`` contract v1.0.0.

Metron already partitions realized lots short- vs long-term with tax treatment (the
"Realized YTD" work, metron-ops#125). This module is a **pure projection** of those
already-computed closed lots into the shape telos's ``engine.reconcile_lots`` consumes —
one Form 8949 row per lot — for a given calendar year, TAXABLE accounts only. It adds no
lot tracking of its own: give it lots + their account/asset context, it emits the contract.

The contract (nousergon/telos ``contracts/realized_lots.schema.json``, generated from
``telos.contracts.RealizedLots``) is the entire coupling — Metron never imports telos code.
A copy of that schema is pinned under ``tests/contracts/`` and a CI test validates a sample
export against it so this export can't silently drift (mirrors the telos generation test).

Box assignment (Form 8949, column-heading part I/II box):
  - Equities/ETFs/funds with basis reported by the broker → **A** (short) / **D** (long) —
    the "basis reported to the IRS" boxes, the default Metron assumes.
  - Crypto (and other digital assets) → the **G–L** family; Metron emits the basis-reported
    box of that family (**F** is not crypto — crypto lives in the second-part G/J vs ...).
    See ``_box`` for the exact letters.
  - When Metron genuinely can't determine the box, it emits the basis-reported default and
    lets telos reconciliation surface any disagreement (fail-loud there, not guess here).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

SCHEMA_VERSION = "1.0.0"

VARIOUS = "VARIOUS"  # 8949 column (b) sentinel for a lot with no single acquisition date

# Term enum (telos ``Term``).
SHORT = "short"
LONG = "long"

# Form 8949 box letters (telos ``Form8949Box`` enum: A–L).
# Part I (short-term):  A basis-reported · B not-reported · C not on a 1099-B
# Part II (long-term):  D basis-reported · E not-reported · F not on a 1099-B
# Digital assets on a 1099-B/DA route through the same three-way, one part each; Metron
# assumes basis-reported (the broker/exchange 1099 default) and defers correction to telos.
BOX_EQUITY_SHORT = "A"
BOX_EQUITY_LONG = "D"
# Crypto short/long, basis-reported — the G–L family the schema allows for digital assets.
BOX_CRYPTO_SHORT = "G"
BOX_CRYPTO_LONG = "J"

_CRYPTO_ASSET_CLASSES = frozenset({"crypto", "cryptocurrency", "digital_asset", "digital asset"})


@dataclass(frozen=True)
class ExportLot:
    """One closed lot to export — the already-computed Metron figures plus the small bit of
    context box/term assignment needs. Native-currency proceeds/basis are the taxpayer's
    reporting currency (USD) figures the rest of Metron carries."""

    description: str            # 8949 (a) — the security (ticker / name)
    date_acquired: date | None  # None ⇒ "VARIOUS" (multiple acquisition dates / unknown)
    date_sold: date             # 8949 (c)
    proceeds: float             # 8949 (d), USD
    cost_basis: float           # 8949 (e), USD
    long_term: bool             # holding period > 1yr (Metron's own classification)
    source: str                 # producing system / connector (e.g. "ibkr_flex")
    asset_class: str | None = None            # Security master asset_class, drives crypto vs equity box
    wash_sale_disallowed: float = 0.0         # broker-reported code-W adjustment; never recomputed


def _is_crypto(asset_class: str | None) -> bool:
    return (asset_class or "").strip().lower() in _CRYPTO_ASSET_CLASSES


def _box(*, long_term: bool, asset_class: str | None) -> str:
    """The basis-reported Form 8949 box for a lot. Crypto → G/J; everything else → A/D.

    Metron always emits the *basis-reported* box (the 1099-B/DA default); telos
    reconciliation flags a real not-reported disagreement — we never guess B/C/E/F/H/I/K/L."""
    if _is_crypto(asset_class):
        return BOX_CRYPTO_LONG if long_term else BOX_CRYPTO_SHORT
    return BOX_EQUITY_LONG if long_term else BOX_EQUITY_SHORT


def _num(x: float) -> float:
    """Round to cents and normalise ``-0.0`` → ``0.0`` (the schema requires ``>= 0`` for the
    numeric branch; a genuine loss shows as a low proceeds vs. basis, never a negative field)."""
    v = round(float(x), 2)
    return 0.0 if v == 0 else v


def lot_to_row(lot: ExportLot) -> dict:
    """Project one lot to a contract ``RealizedLot`` dict (all keys the schema requires)."""
    return {
        "description": lot.description,
        "date_acquired": VARIOUS if lot.date_acquired is None else lot.date_acquired.isoformat(),
        "date_sold": lot.date_sold.isoformat(),
        "proceeds": _num(lot.proceeds),
        "cost_basis": _num(lot.cost_basis),
        "wash_sale_disallowed": _num(lot.wash_sale_disallowed),
        "term": LONG if lot.long_term else SHORT,
        "box": _box(long_term=lot.long_term, asset_class=lot.asset_class),
        "source": lot.source,
    }


def build_export(lots: list[ExportLot]) -> dict:
    """The full contract payload: ``{schema_version, lots: [...]}`` — schema-valid against
    telos ``realized_lots`` v1.0.0. Lots are emitted in the order given (caller sorts)."""
    return {
        "schema_version": SCHEMA_VERSION,
        "lots": [lot_to_row(lot) for lot in lots],
    }
