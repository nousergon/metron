"""add glance latency daily

metron-ops-I327 — Stage A exit gate O2 (glance aggregate endpoint p95 <= 1.0 s over a
trading day). One row per trading day, upserted by ``scripts/glance_p95.py`` from the
router's per-request timing log; ``/meta/status`` reads it so the figure is readable
without an ad-hoc query and its absence renders as "not-measured".

Hand-written, matching the d2a7c5e81f40 precedent. Columns transcribed from
``models.GlanceLatencyDaily``.

Revision ID: 2f5fac711c62
Revises: d2a7c5e81f40
Create Date: 2026-09-16

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '2f5fac711c62'
down_revision: Union[str, Sequence[str], None] = 'd2a7c5e81f40'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'glance_latency_daily',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('trading_day', sa.Date(), nullable=False),
        sa.Column('p95_ms', sa.Numeric(10, 2), nullable=False),
        sa.Column('n', sa.Integer(), nullable=False),
        sa.Column('computed_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('trading_day'),
    )
    op.create_index(
        op.f('ix_glance_latency_daily_trading_day'), 'glance_latency_daily', ['trading_day'], unique=True
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_glance_latency_daily_trading_day'), table_name='glance_latency_daily')
    op.drop_table('glance_latency_daily')
