"""add external demo invites and sessions

metron-ops-I310 — the invite-gated external user demo. Single-use invite codes and the
server-side sessions they mint, both stored as SHA-256 digests only. Operator-level
tables, not tenant-scoped (no tenant data is reachable through them).

Hand-written, matching the c4e8f2a9d6b1 precedent. Columns transcribed from
``models.ExternalDemoInvite`` and ``models.ExternalDemoSession``.

Revision ID: d2a7c5e81f40
Revises: c4e8f2a9d6b1
Create Date: 2026-09-15

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd2a7c5e81f40'
down_revision: Union[str, Sequence[str], None] = 'c4e8f2a9d6b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'external_demo_invites',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('code_digest', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('redeemed_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code_digest'),
    )
    op.create_table(
        'external_demo_sessions',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('token_digest', sa.String(length=64), nullable=False),
        sa.Column('invite_id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['invite_id'], ['external_demo_invites.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('token_digest'),
    )
    op.create_index(
        op.f('ix_external_demo_sessions_invite_id'), 'external_demo_sessions', ['invite_id'], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_external_demo_sessions_invite_id'), table_name='external_demo_sessions')
    op.drop_table('external_demo_sessions')
    op.drop_table('external_demo_invites')
