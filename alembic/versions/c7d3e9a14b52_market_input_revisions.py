"""market input revisions

metron-ops-I370 — a revised close or FX rate no longer erases the value it replaces.

* ``market_input_revisions``: the append-only record of every ``price_bars.close`` and
  ``fx_rates.rate`` that was overwritten or deleted. Written by
  ``api.services.prices._record_superseded_bars`` (the Core backfill upsert) and by the
  ``before_flush`` hook ``api.db.models._record_input_revisions`` (every ORM write). No FK
  to ``securities`` on purpose, so a revision outlives the row it describes.

Additive only: no existing row is read, rewritten or deleted by this migration.

Hand-written, matching the e5a17c3f9b20 precedent. Columns transcribed from
``models.MarketInputRevision``.

Revision ID: c7d3e9a14b52
Revises: b4e9d2c6f813
Create Date: 2026-11-06

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c7d3e9a14b52'
down_revision: Union[str, Sequence[str], None] = 'b4e9d2c6f813'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEXED = ('security_id', 'currency', 'obs_date')


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'market_input_revisions',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('kind', sa.String(8), nullable=False),
        sa.Column('security_id', sa.Uuid(), nullable=True),
        sa.Column('currency', sa.String(3), nullable=True),
        sa.Column('base', sa.String(3), nullable=True),
        sa.Column('obs_date', sa.Date(), nullable=False),
        sa.Column('value', sa.Numeric(precision=28, scale=10), nullable=False),
        sa.Column('change', sa.String(16), nullable=False),
        sa.Column('superseded_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    for col in _INDEXED:
        op.create_index(op.f(f'ix_market_input_revisions_{col}'), 'market_input_revisions', [col], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    for col in _INDEXED:
        op.drop_index(op.f(f'ix_market_input_revisions_{col}'), table_name='market_input_revisions')
    op.drop_table('market_input_revisions')
