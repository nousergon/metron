"""account holdings / open-lot sync stamps

metron-ops-I382 (audit FIN-01): an authoritative empty broker snapshot left sold-out
positions and open lots alive, because replacement scope came from the rows present and
``Position.as_of`` was the only sync stamp. Persistence now replaces per stated account
and stamps each one, empty or not.

* ``accounts.holdings_synced_on``: the last successful snapshot that stated the account's
  holdings in full. Nullable; NULL = never stated since this shipped.
* ``accounts.open_lots_synced_on``: the same for its open-lot set.

Additive only: no existing row is read, rewritten or deleted, so every existing account
stays NULL until its next stated sync.

Hand-written, matching the f3b81d6c2a47 precedent. Columns transcribed from
``models.Account``.

Revision ID: a8c3e5f71d24
Revises: f3b81d6c2a47
Create Date: 2026-10-31

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a8c3e5f71d24'
down_revision: Union[str, Sequence[str], None] = 'f3b81d6c2a47'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('accounts', sa.Column('holdings_synced_on', sa.Date(), nullable=True))
    op.add_column('accounts', sa.Column('open_lots_synced_on', sa.Date(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('accounts', 'open_lots_synced_on')
    op.drop_column('accounts', 'holdings_synced_on')
