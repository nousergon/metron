"""account identity scope: portfolio-local CSV/manual labels

metron-ops-I383 (audit FIN-02): ``uq_account_source`` made ``(tenant_id, broker,
external_id)`` unique tenant-wide for every source, so a free-text CSV/manual label such
as the default "CSV" named ONE account per tenant and a second portfolio's import was
written into the first portfolio's account.

* ``accounts.identity_scope``: ``tenant`` for sources whose external id is a
  brokerage-assigned account number, or the creating portfolio's id (32-hex) for
  ``csv``/``manual`` labels. Mirrors ``models.account_identity_scope``.
* ``uq_account_source`` is replaced by ``uq_account_identity`` over
  ``(tenant_id, broker, external_id, identity_scope)``.

Conservative by construction: every existing row keeps its id, portfolio, values and
foreign keys, and each CSV/manual row is scoped to the portfolio it is parented to
now. Nothing is merged, split or moved. What this cannot know is whether an existing
CSV/manual account already absorbed an import aimed at a different portfolio of the
same tenant: no import ever recorded its target. So the upgrade does not guess. It logs
every CSV/manual account in a tenant with more than one portfolio as AMBIGUOUS, for
explicit resolution, and ``python -m api.services.account_identity`` prints the same
report, read-only, at any time.

Downgrade restores the tenant-wide constraint and refuses, naming the count, when
labels have since diverged per portfolio, because it would have to merge them.

Revision ID: b4e9d2c6f813
Revises: a8c3e5f71d24
Create Date: 2026-10-31

"""
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b4e9d2c6f813'
down_revision: Union[str, Sequence[str], None] = 'a8c3e5f71d24'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_LABEL_SOURCES = "('csv', 'manual')"
_log = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('accounts', sa.Column('identity_scope', sa.String(length=64), nullable=True))
    op.execute(
        "UPDATE accounts SET identity_scope = CASE "
        f"WHEN broker IN {_LABEL_SOURCES} THEN replace(CAST(portfolio_id AS TEXT), '-', '') "
        "ELSE 'tenant' END"
    )
    op.alter_column('accounts', 'identity_scope', existing_type=sa.String(length=64), nullable=False)
    op.drop_constraint('uq_account_source', 'accounts', type_='unique')
    op.create_unique_constraint(
        'uq_account_identity', 'accounts', ['tenant_id', 'broker', 'external_id', 'identity_scope']
    )
    rows = op.get_bind().execute(
        sa.text(
            "SELECT a.id, a.broker FROM accounts a "
            f"WHERE a.broker IN {_LABEL_SOURCES} "
            "AND (SELECT count(*) FROM portfolios p WHERE p.tenant_id = a.tenant_id) > 1 "
            "ORDER BY a.id"
        )
    ).all()
    if rows:
        _log.warning(
            "metron-ops#383: %d CSV/manual account(s) are AMBIGUOUS: their tenant has more "
            "than one portfolio, so imports aimed at another portfolio may have been "
            "written into them. Left exactly as they were; resolve each explicitly "
            "(python -m api.services.account_identity). Account ids: %s",
            len(rows),
            ", ".join(str(r.id) for r in rows),
        )


def downgrade() -> None:
    """Downgrade schema."""
    clashes = op.get_bind().execute(
        sa.text(
            "SELECT count(*) FROM (SELECT tenant_id, broker, external_id FROM accounts "
            "GROUP BY tenant_id, broker, external_id HAVING count(*) > 1) d"
        )
    ).scalar()
    if clashes:
        raise RuntimeError(
            f"{clashes} account label(s) now exist in more than one portfolio; restoring "
            "the tenant-wide uq_account_source would merge them. Resolve them first."
        )
    op.drop_constraint('uq_account_identity', 'accounts', type_='unique')
    op.create_unique_constraint('uq_account_source', 'accounts', ['tenant_id', 'broker', 'external_id'])
    op.drop_column('accounts', 'identity_scope')
