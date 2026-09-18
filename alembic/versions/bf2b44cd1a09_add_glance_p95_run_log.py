"""add glance p95 run log

metron-ops-I341 — the execution record for `scripts/glance_p95.py --record`,
distinct from `GlanceLatencyDaily`'s data record. Every invocation appends a row here
(status measured / not-measured / error) so "the scheduled timer never fired" is
distinguishable from "it fired and found no traffic that day" — both currently read
identically as an absent `GlanceLatencyDaily` row.

Hand-written, matching the 2f5fac711c62 precedent. Columns transcribed from
``models.GlanceP95RunLog``.

Revision ID: bf2b44cd1a09
Revises: 2f5fac711c62
Create Date: 2026-09-18

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'bf2b44cd1a09'
down_revision: Union[str, Sequence[str], None] = '2f5fac711c62'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'glance_p95_run_log',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('ran_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('target_day', sa.Date(), nullable=False),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('n', sa.Integer(), nullable=True),
        sa.Column('p95_ms', sa.Numeric(10, 2), nullable=True),
        sa.Column('error', sa.String(2000), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_glance_p95_run_log_ran_at'), 'glance_p95_run_log', ['ran_at'], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_glance_p95_run_log_ran_at'), table_name='glance_p95_run_log')
    op.drop_table('glance_p95_run_log')
