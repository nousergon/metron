"""YTD realized split on the portfolio summary (Overview cards).

``analytics.summary`` surfaces realized gains scoped to the current calendar year and
split by tax treatment, so the Overview can show "Taxable realized YTD" (with the ST/LT
breakdown — the tax-relevant figure) alongside "Tax-advantaged realized YTD" (never
taxed), mirroring how it splits unrealized. All-time ``realized_total`` is unchanged.
"""

from __future__ import annotations

from datetime import date

from api.db import models
from api.services import analytics


def _seed_portfolio(session):
    tenant = models.Tenant(name="t-ytd")
    session.add(tenant)
    session.flush()
    pf = models.Portfolio(tenant_id=tenant.id, name="P", base_currency="USD")
    session.add(pf)
    session.flush()
    return tenant, pf


def _account(session, tenant, pf, external_id: str, *, tax_treatment: str) -> models.Account:
    acct = models.Account(
        tenant_id=tenant.id, portfolio_id=pf.id, broker="csv",
        external_id=external_id, currency="USD", tax_treatment=tax_treatment,
    )
    session.add(acct)
    session.flush()
    return acct


def _txn(session, tenant, acct, sec, *, txn_type: str, qty: float, price: float, when: date, key: str):
    session.add(
        models.Transaction(
            tenant_id=tenant.id, account_id=acct.id, security_id=sec.id,
            txn_type=txn_type, quantity=qty, price=price, amount=qty * price,
            currency="USD", trade_date=when, source_key=key,
        )
    )


def test_summary_splits_realized_ytd_by_tax_treatment(db_session):
    this_year = date.today().year
    tenant, pf = _seed_portfolio(db_session)
    aapl = models.Security(symbol="AAPL", currency="USD")
    msft = models.Security(symbol="MSFT", currency="USD")
    nvda = models.Security(symbol="NVDA", currency="USD")
    spy = models.Security(symbol="SPY", currency="USD")
    db_session.add_all([aapl, msft, nvda, spy])
    db_session.flush()

    taxable = _account(db_session, tenant, pf, "BRK-1", tax_treatment="taxable")
    ira = _account(db_session, tenant, pf, "IRA-1", tax_treatment="tax_exempt")  # Roth-style, never taxed

    # Taxable, SHORT-TERM, YTD: bought + sold this year, +$500.
    _txn(db_session, tenant, taxable, aapl, txn_type="BUY", qty=10, price=100, when=date(this_year, 1, 6), key="a-b")
    _txn(db_session, tenant, taxable, aapl, txn_type="SELL", qty=10, price=150, when=date(this_year, 2, 6), key="a-s")
    # Taxable, LONG-TERM, YTD: bought >1yr ago, sold this year, +$1000.
    _txn(db_session, tenant, taxable, msft, txn_type="BUY", qty=10, price=100, when=date(this_year - 2, 1, 6), key="m-b")
    _txn(db_session, tenant, taxable, msft, txn_type="SELL", qty=10, price=200, when=date(this_year, 1, 20), key="m-s")
    # Taxable, PRIOR-YEAR (not YTD): closed two years ago, +$300 — in all-time, not YTD.
    _txn(db_session, tenant, taxable, nvda, txn_type="BUY", qty=10, price=100, when=date(this_year - 2, 1, 6), key="n-b")
    _txn(db_session, tenant, taxable, nvda, txn_type="SELL", qty=10, price=130, when=date(this_year - 2, 6, 6), key="n-s")
    # Tax-advantaged (IRA), YTD: +$800 — never taxed, so it lands in the tax-advantaged total only.
    _txn(db_session, tenant, ira, spy, txn_type="BUY", qty=10, price=100, when=date(this_year, 1, 6), key="s-b")
    _txn(db_session, tenant, ira, spy, txn_type="SELL", qty=10, price=180, when=date(this_year, 3, 6), key="s-s")
    db_session.commit()

    s = analytics.summary(db_session, tenant.id, pf.id)

    # Taxable YTD split: ST = AAPL (+500), LT = MSFT (+1000). NVDA's prior-year +300 is excluded.
    assert s.realized_st_ytd == 500
    assert s.realized_lt_ytd == 1000
    # Tax-advantaged YTD: IRA SPY (+800) only — and it never bleeds into the taxable figures.
    assert s.realized_ytd_taxadv == 800
    # All-time realized still includes every closed lot across treatments (500+1000+300+800).
    assert s.realized_total == 2600


def test_summary_realized_ytd_zero_when_no_current_year_lots(db_session):
    this_year = date.today().year
    tenant, pf = _seed_portfolio(db_session)
    aapl = models.Security(symbol="AAPL", currency="USD")
    db_session.add(aapl)
    db_session.flush()
    taxable = _account(db_session, tenant, pf, "BRK-1", tax_treatment="taxable")
    # Only a prior-year realized gain — YTD fields stay zero, all-time reflects it.
    _txn(db_session, tenant, taxable, aapl, txn_type="BUY", qty=10, price=100, when=date(this_year - 3, 1, 6), key="a-b")
    _txn(db_session, tenant, taxable, aapl, txn_type="SELL", qty=10, price=140, when=date(this_year - 3, 6, 6), key="a-s")
    db_session.commit()

    s = analytics.summary(db_session, tenant.id, pf.id)
    assert s.realized_st_ytd == 0
    assert s.realized_lt_ytd == 0
    assert s.realized_ytd_taxadv == 0
    assert s.realized_total == 400
