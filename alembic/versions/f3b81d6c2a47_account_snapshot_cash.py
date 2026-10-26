"""account snapshot cash and its source

metron-ops-I371 (MET-03 slice 3) — the account-basis performance engine needs each
account's cash on the same day as its holdings NAV, and where that cash came from. Per-
account history accrues forward only, so the nightly recorder starts writing it now.

* ``account_nav_snapshots.cash_usd``: the account's cash in base currency on the snapshot
  day. Nullable; NULL = not observed, never zero.
* ``account_nav_snapshots.cash_source``: ``custodian`` (the connector-reported balance) or
  ``ledger`` (accrued from the transaction ledger). Nullable.

Additive only: no existing row is read, rewritten or deleted by this migration, so every
row written before it stays NULL (unobserved), which is what it was.

Hand-written, matching the bf2b44cd1a09 precedent. Columns transcribed from
``models.AccountNavSnapshot``.

Revision ID: f3b81d6c2a47
Revises: e5a17c3f9b20
Create Date: 2026-10-26

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f3b81d6c2a47'
down_revision: Union[str, Sequence[str], None] = 'e5a17c3f9b20'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('account_nav_snapshots', sa.Column('cash_usd', sa.Numeric(28, 10), nullable=True))
    op.add_column('account_nav_snapshots', sa.Column('cash_source', sa.String(length=16), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('account_nav_snapshots', 'cash_source')
    op.drop_column('account_nav_snapshots', 'cash_usd')
