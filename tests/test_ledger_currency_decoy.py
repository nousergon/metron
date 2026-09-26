"""metron-ops#351: tax lots, the "if sold" preview and realized gains take a ticker's
currency from the Security row the tenant's own ledger links to.

``securities`` is a global table keyed ``(symbol, currency)``. These views used to read a
ticker's currency from the global "first Security row per symbol" pick, so a same-symbol
row under another currency (another tenant's, or a stray) decided which currency this
tenant's lots and gains were in. Same decoy shape as
``test_intraday.TestLedgerOnlyDecoyCannotHijackCurrency``.
"""

from __future__ import annotations

import uuid
from datetime import date

from api.db import models
from api.services import analytics, tax
from portfolio_analytics.domain.tax import TaxRates

TODAY = date(2026, 6, 15)


def _seed(session):
    tenant = models.Tenant(name="t")
    session.add(tenant)
    session.flush()
    pf = models.Portfolio(tenant_id=tenant.id, name="P", base_currency="HKD")
    session.add(pf)
    session.flush()
    acct = models.Account(tenant_id=tenant.id, portfolio_id=pf.id, broker="csv", external_id="C1", currency="HKD")
    real = models.Security(symbol="1299", yf_symbol="1299.HK", currency="HKD")
    # Sorts first by id, so the global symbol-text pick lands on it.
    decoy = models.Security(id=uuid.UUID(int=0), symbol="1299", currency="USD", yf_symbol=None)
    session.add_all([acct, real, decoy])
    session.flush()
    session.add_all(
        [
            models.Transaction(
                tenant_id=tenant.id, account_id=acct.id, security_id=real.id, txn_type="BUY",
                quantity=100, price=60, amount=6000, currency="HKD",
                trade_date=date(2026, 6, 1), source_key="buy-1299",
            ),
            models.Transaction(
                tenant_id=tenant.id, account_id=acct.id, security_id=real.id, txn_type="SELL",
                quantity=40, price=70, amount=2800, currency="HKD",
                trade_date=date(2026, 6, 10), source_key="sell-1299",
            ),
            models.PriceBar(security_id=real.id, bar_date=date(2026, 6, 11), close=75.0, currency="HKD"),
        ]
    )
    session.commit()
    return tenant.id, pf.id, acct.id


def test_precondition_the_global_pick_is_the_decoy(db_session):
    """Else every test below would pass against the old code too."""
    _seed(db_session)
    assert analytics._currency_by_symbol(db_session, ["1299"]) == {"1299": "USD"}


def test_ledger_lookup_prefers_the_tenants_own_link(db_session):
    tid, _pid, _aid = _seed(db_session)
    assert analytics._ledger_currency_by_symbol(db_session, tid, ["1299"]) == {"1299": "HKD"}


def test_a_symbol_with_no_tenant_link_keeps_the_global_fallback(db_session):
    tid, _pid, _aid = _seed(db_session)
    db_session.add(models.Security(symbol="ZZZ", currency="EUR"))
    db_session.commit()
    assert analytics._ledger_currency_by_symbol(db_session, tid, ["ZZZ"]) == {"ZZZ": "EUR"}


def test_tax_lots_are_in_the_ledgers_currency(db_session):
    tid, pid, _aid = _seed(db_session)
    summary = tax.tax_lots(db_session, tid, pid, today=TODAY, taxable_only=False)
    assert summary.lots
    assert {lot.currency for lot in summary.lots} == {"HKD"}


def test_if_sold_preview_is_in_the_ledgers_currency(db_session):
    tid, pid, _aid = _seed(db_session)
    preview = tax.if_sold_preview(
        db_session, tid, pid, "1299", today=TODAY, taxable_only=False,
        rates=TaxRates(short_term=0.30, long_term=0.15, state=0.05),
    )
    assert preview.currency == "HKD"


def test_realized_gains_are_in_the_ledgers_currency(db_session):
    tid, pid, _aid = _seed(db_session)
    lots = analytics._realized(db_session, tid, pid)
    assert lots
    assert {lot.currency for lot in lots} == {"HKD"}
