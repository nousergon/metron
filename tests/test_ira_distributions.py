"""Tax-deferred distributions as taxable ordinary income (metron-ops#62).

"Trad IRA is still taxable for retirees": a withdrawal from a tax-DEFERRED account
(Trad IRA / 401(k), incl. RMDs) is taxable ordinary income, even though the account's
internal gains/dividends aren't taxed annually. A withdrawal from a tax-EXEMPT account
(Roth / HSA) or a plain taxable brokerage is NOT distribution income."""

from __future__ import annotations

import io
import uuid

import pytest

from api.db import models
from api.services import account_meta

# Trad IRA (tax-deferred) + Brokerage (taxable) + Roth IRA (tax-exempt). The IRA takes a
# $5,000 withdrawal in 2024; the brokerage and Roth each take one too (must NOT count).
CSV = (
    "date,type,symbol,quantity,price,amount,account\n"
    "2024-01-02,BUY,MSFT,10,200,2000,Trad IRA\n"
    "2024-03-01,DIVIDEND,MSFT,0,0,30,Trad IRA\n"
    "2024-07-01,WITHDRAWAL,,0,0,5000,Trad IRA\n"
    "2024-01-02,BUY,AAPL,10,100,1000,Brokerage\n"
    "2024-08-01,WITHDRAWAL,,0,0,1200,Brokerage\n"
    "2024-09-01,WITHDRAWAL,,0,0,800,Roth IRA\n"
)


@pytest.fixture()
def tenant() -> str:
    return str(uuid.uuid4())


def _hdr(t: str) -> dict:
    return {"X-Tenant-Id": t}


def _seed(client, tenant: str) -> str:
    pid = client.post("/portfolios", json={"name": "P"}, headers=_hdr(tenant)).json()["id"]
    r = client.post(
        f"/portfolios/{pid}/import/csv",
        files={"file": ("t.csv", io.BytesIO(CSV.encode()), "text/csv")},
        headers=_hdr(tenant),
    )
    assert r.status_code == 200
    return pid


# --- classification (pure) --------------------------------------------------------------

def _acct(*, name="", account_type="", treatment=None, override=None) -> models.Account:
    return models.Account(name=name, account_type=account_type, tax_treatment=treatment, taxable_override=override)


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({"name": "Trad IRA"}, True),
        ({"name": "Rollover IRA"}, True),
        ({"account_type": "401k"}, True),
        ({"name": "My 403(b)"}, True),
        ({"name": "Roth IRA"}, False),  # exempt keyword wins over the bare "ira"
        ({"name": "HSA"}, False),
        ({"name": "529 plan"}, False),
        ({"name": "Brokerage"}, False),
        ({"treatment": "tax_deferred"}, True),  # connector tag is authoritative
        ({"treatment": "tax_exempt", "name": "Trad IRA"}, False),  # tag overrides keyword
        ({"override": True, "name": "Trad IRA"}, False),  # manual taxable → never deferred
    ],
)
def test_is_tax_deferred(kwargs, expected):
    assert account_meta.is_tax_deferred(_acct(**kwargs)) is expected


# --- income wiring (API) ----------------------------------------------------------------

def test_ira_withdrawal_is_taxable_distribution(client, tenant):
    pid = _seed(client, tenant)
    income = client.get(f"/portfolios/{pid}/income", headers=_hdr(tenant)).json()
    by_year = {y["year"]: y for y in income}
    # Only the Trad IRA's $5,000 withdrawal is a distribution; brokerage + Roth excluded.
    assert by_year[2024]["distributions"] == pytest.approx(5000)


def test_distributions_survive_taxable_only_filter(client, tenant):
    """taxable_only restricts gains/dividends to taxable accounts, but tax-deferred
    distributions are the retiree's taxable income and must still show."""
    pid = _seed(client, tenant)
    only = client.get(f"/portfolios/{pid}/income?taxable_only=true", headers=_hdr(tenant)).json()
    by_year = {y["year"]: y for y in only}
    assert by_year[2024]["distributions"] == pytest.approx(5000)
    # The IRA's dividend is still excluded from the taxable dividend column.
    assert by_year[2024]["dividends"] == pytest.approx(0)


def test_distributions_fold_into_taxable_income(client, tenant):
    pid = _seed(client, tenant)
    income = client.get(f"/portfolios/{pid}/income?taxable_only=true", headers=_hdr(tenant)).json()
    y = {row["year"]: row for row in income}[2024]
    # taxable_income = cap gains (0) + dividends (0) + interest (0) + distributions (5000).
    assert y["taxable_income"] == pytest.approx(
        y["net_capital_gains"] + y["dividends"] + y["interest"] + y["distributions"]
    )
    assert y["taxable_income"] == pytest.approx(5000)


def test_non_deferred_withdrawals_are_not_distributions(client, tenant):
    """A plain brokerage / Roth withdrawal is moving cash, not taxable income."""
    pid = _seed(client, tenant)
    income = client.get(f"/portfolios/{pid}/income", headers=_hdr(tenant)).json()
    # Total distributions across all years = just the Trad IRA's $5,000.
    assert sum(y["distributions"] for y in income) == pytest.approx(5000)
