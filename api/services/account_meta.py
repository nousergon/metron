"""Account classification — is a connected account *taxable*?

The Tax lens (unrealized P&L, harvestable losses, taxable income) is meaningful only
for taxable accounts: gains inside an IRA / 401(k) / Roth / HSA are never realized for
tax, so folding them into a harvestable-loss or taxable-income figure is misleading.

Classification is **auto-derived from the connector's metadata, with a manual
override**: a Settings-set ``Account.taxable_override`` wins; otherwise we infer from
``tax_treatment`` (the FDX-style tag the connector carries) and, failing that, from
keywords in ``account_type`` / ``name``. Default when nothing is known: **taxable**
(the conservative choice — better to show a lot than to silently hide it).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db import models

# tax_treatment values that mean "not taxable" (FDX-style; connector-supplied).
_NON_TAXABLE_TREATMENTS = {"tax_deferred", "tax_exempt", "tax-deferred", "tax-exempt", "retirement"}

# account_type / name keywords that imply a tax-advantaged wrapper. Matched
# case-insensitively as whole-ish tokens against the combined type+name string.
_NON_TAXABLE_KEYWORDS = (
    "ira", "roth", "401k", "401(k)", "403b", "403(b)", "457", "hsa", "529",
    "rrsp", "tfsa", "sep", "simple ira", "pension", "annuity", "retirement",
)

# Tax-DEFERRED wrappers (Trad IRA / 401(k) / 403(b) / SEP / pension …). Distinct from
# tax-EXEMPT (Roth / HSA / 529): contributions/internal growth aren't taxed annually,
# but DISTRIBUTIONS (withdrawals, incl. RMDs at 73+) are taxable ordinary income — the
# "Trad IRA is still taxable for retirees" point (metron-ops#62). The connector tag wins;
# keyword inference checks the exempt set first so "roth ira" never matches the bare "ira".
_TAX_DEFERRED_TREATMENTS = {"tax_deferred", "tax-deferred", "retirement"}
_TAX_EXEMPT_KEYWORDS = ("roth", "hsa", "529", "tfsa")
_TAX_DEFERRED_KEYWORDS = (
    "ira", "401k", "401(k)", "403b", "403(b)", "457", "rrsp", "sep",
    "simple ira", "pension", "annuity",
)


def is_taxable(account: models.Account) -> bool:
    """Whether ``account`` is a taxable brokerage account.

    Precedence: explicit ``taxable_override`` → ``tax_treatment`` → keyword inference on
    ``account_type``/``name`` → default True."""
    if account.taxable_override is not None:
        return bool(account.taxable_override)
    treatment = (account.tax_treatment or "").strip().lower()
    if treatment in _NON_TAXABLE_TREATMENTS:
        return False
    if treatment == "taxable":
        return True
    haystack = f"{account.account_type or ''} {account.name or ''}".lower()
    if any(kw in haystack for kw in _NON_TAXABLE_KEYWORDS):
        return False
    return True


def is_tax_deferred(account: models.Account) -> bool:
    """Whether ``account`` is a tax-DEFERRED wrapper (Trad IRA / 401(k) / 403(b) / SEP /
    pension) — withdrawals from it are taxable ordinary income (metron-ops#62).

    A tax-EXEMPT wrapper (Roth / HSA / 529) returns False: its qualified distributions
    are tax-free. Precedence: connector ``tax_treatment`` tag → keyword inference (exempt
    keywords checked first, so "roth ira" is exempt, not deferred) → default False.

    A manual ``taxable_override=True`` forces taxable, so never deferred. An override of
    False only says "not taxable" — it can't tell deferred from exempt — so we fall
    through to the tag/keyword inference to decide which."""
    if account.taxable_override is True:
        return False
    treatment = (account.tax_treatment or "").strip().lower()
    if treatment in _TAX_DEFERRED_TREATMENTS:
        return True
    if treatment in {"tax_exempt", "tax-exempt", "taxable"}:
        return False
    haystack = f"{account.account_type or ''} {account.name or ''}".lower()
    if any(kw in haystack for kw in _TAX_EXEMPT_KEYWORDS):
        return False
    return any(kw in haystack for kw in _TAX_DEFERRED_KEYWORDS)


def taxable_account_ids(session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID) -> set[uuid.UUID]:
    """The ids of a portfolio's taxable accounts (per ``is_taxable``)."""
    rows = session.scalars(
        select(models.Account).where(
            models.Account.tenant_id == tenant_id, models.Account.portfolio_id == portfolio_id
        )
    ).all()
    return {a.id for a in rows if is_taxable(a)}


def tax_deferred_account_ids(session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID) -> set[uuid.UUID]:
    """The ids of a portfolio's tax-deferred accounts (per ``is_tax_deferred``) — the
    accounts whose withdrawals count as taxable distributions."""
    rows = session.scalars(
        select(models.Account).where(
            models.Account.tenant_id == tenant_id, models.Account.portfolio_id == portfolio_id
        )
    ).all()
    return {a.id for a in rows if is_tax_deferred(a)}
