"""add retirement_goal

metron-ops-I316 — the retire-early goal facets. One user-authored goal per
portfolio: target amount, target date, planned annual contribution, planned
withdrawal rate. Every column is nullable (no defaults, no pre-fill) — the doctrine
layer-2 exemption (positioning §3c.2) holds only because the numbers are the
user's own.

Hand-written, matching the a1f2c3d4e5b6/b7d3e91a4c2f precedent: autogenerate would
have to run against Neon from a machine that can reach it. Columns transcribed
from ``models.RetirementGoal``.

Revision ID: c4e8f2a9d6b1
Revises: c9e4f27a1b83
Create Date: 2026-09-15

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c4e8f2a9d6b1'
down_revision: Union[str, Sequence[str], None] = 'c9e4f27a1b83'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'retirement_goal',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.Column('portfolio_id', sa.Uuid(), nullable=False),
        sa.Column('target_amount_usd', sa.Numeric(28, 2), nullable=True),
        sa.Column('target_date', sa.Date(), nullable=True),
        sa.Column('annual_contribution_usd', sa.Numeric(28, 2), nullable=True),
        sa.Column('withdrawal_rate', sa.Numeric(6, 4), nullable=True),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['portfolio_id'], ['portfolios.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tenant_id', 'portfolio_id', name='uq_retirement_goal_portfolio'),
    )
    op.create_index(
        op.f('ix_retirement_goal_portfolio_id'), 'retirement_goal', ['portfolio_id'], unique=False
    )
    op.create_index(
        op.f('ix_retirement_goal_tenant_id'), 'retirement_goal', ['tenant_id'], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_retirement_goal_tenant_id'), table_name='retirement_goal')
    op.drop_index(op.f('ix_retirement_goal_portfolio_id'), table_name='retirement_goal')
    op.drop_table('retirement_goal')
