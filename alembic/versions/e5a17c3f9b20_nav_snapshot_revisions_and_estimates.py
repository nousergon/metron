"""nav snapshot revisions and estimate stamps

metron-ops-I370 — a dated valuation is no longer erased when a writer revises it, and an
estimated historical valuation says so.

* ``nav_snapshot_revisions``: the append-only record of every value a portfolio- or
  account-grain NAV snapshot row served before it was changed or deleted. Written by the
  ``before_flush`` hook in ``api.db.models`` (``_record_nav_revisions``). ``snapshot_id``
  carries no FK on purpose, so a revision outlives a deleted snapshot row.
* ``nav_snapshots.estimated_inputs``: the legs of a reconstructed NAV that were not valued
  from inputs dated on/before ``snap_date``. Nullable; NULL = exact.

Additive only: no existing row is read, rewritten or deleted by this migration.

Hand-written, matching the bf2b44cd1a09 precedent. Columns transcribed from
``models.NavSnapshotRevision`` / ``models.NavSnapshot``.

Revision ID: e5a17c3f9b20
Revises: bf2b44cd1a09
Create Date: 2026-10-23

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e5a17c3f9b20'
down_revision: Union[str, Sequence[str], None] = 'bf2b44cd1a09'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('nav_snapshots', sa.Column('estimated_inputs', sa.JSON(none_as_null=True), nullable=True))
    op.create_table(
        'nav_snapshot_revisions',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.Column('portfolio_id', sa.Uuid(), nullable=False),
        sa.Column('account_id', sa.Uuid(), nullable=True),
        sa.Column('kind', sa.String(16), nullable=False),
        sa.Column('snapshot_id', sa.Uuid(), nullable=False),
        sa.Column('snap_date', sa.Date(), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('change', sa.String(16), nullable=False),
        sa.Column('nav', sa.Numeric(precision=28, scale=10), nullable=False),
        sa.Column('cost_basis', sa.Numeric(precision=28, scale=10), nullable=True),
        sa.Column('external_flow', sa.Numeric(precision=28, scale=10), nullable=True),
        sa.Column('spy_close', sa.Numeric(precision=28, scale=10), nullable=True),
        sa.Column('provisional', sa.Boolean(), nullable=True),
        sa.Column('composition', sa.JSON(), nullable=True),
        sa.Column('estimated_inputs', sa.JSON(none_as_null=True), nullable=True),
        sa.Column('recorded_at', sa.DateTime(), nullable=True),
        sa.Column('superseded_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['portfolio_id'], ['portfolios.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    for col in ('tenant_id', 'portfolio_id', 'account_id', 'snapshot_id', 'snap_date'):
        op.create_index(
            op.f(f'ix_nav_snapshot_revisions_{col}'), 'nav_snapshot_revisions', [col], unique=False
        )


def downgrade() -> None:
    """Downgrade schema."""
    for col in ('tenant_id', 'portfolio_id', 'account_id', 'snapshot_id', 'snap_date'):
        op.drop_index(op.f(f'ix_nav_snapshot_revisions_{col}'), table_name='nav_snapshot_revisions')
    op.drop_table('nav_snapshot_revisions')
    op.drop_column('nav_snapshots', 'estimated_inputs')
