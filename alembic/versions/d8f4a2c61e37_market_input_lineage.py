"""market input lineage

metron-ops-I370 / metron-ops-I373 — every cached close and FX rate names the producer
object version it was read from and when Metron recorded it, and a superseded value keeps
that lineage in ``market_input_revisions``.

* ``price_bars``: ``source_ref``, ``adjustment_basis``, ``recorded_at``
* ``fx_rates``: ``source_ref``, ``recorded_at``
* ``market_input_revisions``: ``source_ref``, ``adjustment_basis``, ``recorded_at``

Additive only: every column is nullable and no existing row is read, rewritten or
deleted. A row cached before this migration keeps NULL lineage; nothing guesses one.

Hand-written, matching the c7d3e9a14b52 precedent. Columns transcribed from
``models.PriceBar``, ``models.FxRate`` and ``models.MarketInputRevision``.

Revision ID: d8f4a2c61e37
Revises: c7d3e9a14b52
Create Date: 2026-11-14

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd8f4a2c61e37'
down_revision: Union[str, Sequence[str], None] = 'c7d3e9a14b52'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS = {
    'price_bars': ('source_ref', 'adjustment_basis', 'recorded_at'),
    'fx_rates': ('source_ref', 'recorded_at'),
    'market_input_revisions': ('source_ref', 'adjustment_basis', 'recorded_at'),
}


def _column(name: str) -> sa.Column:
    if name == 'source_ref':
        return sa.Column('source_ref', sa.String(512), nullable=True)
    if name == 'adjustment_basis':
        return sa.Column('adjustment_basis', sa.String(32), nullable=True)
    return sa.Column('recorded_at', sa.DateTime(), nullable=True)


def upgrade() -> None:
    """Upgrade schema."""
    for table, columns in _COLUMNS.items():
        for name in columns:
            op.add_column(table, _column(name))


def downgrade() -> None:
    """Downgrade schema."""
    for table, columns in _COLUMNS.items():
        for name in reversed(columns):
            op.drop_column(table, name)
