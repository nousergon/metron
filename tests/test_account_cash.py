"""Cash/sweep balance surfacing — the root-cause fix for a connector-computed cash
figure being silently dropped before persistence (metron-ops; live case: $20.3k
missing from the Crucible reference-rate sleeve's displayed total).

Two sources feed ``AccountInfo.cash`` / ``PortfolioSummary.cash``, never both for the
same account (mirrors the ledger-XOR-broker-snapshot split ``_holdings`` already
enforces for shares):

  * snapshot-sourced (Flex/SnapTrade/reference) — the persisted, connector-reported
    ``Account.cash_balance_usd`` (``_upsert_accounts`` in persistence.py).
  * ledger-sourced (CSV/OFX/manual) — accrued from the transaction ledger's
    deposit/withdrawal/dividend/interest/fee/buy/sell arithmetic
    (``analytics._ledger_cash_by_account``).

Deliberately kept SEPARATE from ``market_value`` (pure holdings valuation) rather than
folded in, so ``unrealized_gain = market_value - cost_basis_base`` keeps holding
elsewhere (Tax page, per-holding P&L) — see analytics.AccountInfo/PortfolioSummary.
"""

from __future__ import annotations

from datetime import date

from api.db import models
from api.services import analytics


def _seed_portfolio(session):
    tenant = models.Tenant(name="t-cash")
    session.add(tenant)
    session.flush()
    pf = models.Portfolio(tenant_id=tenant.id, name="P", base_currency="USD")
    session.add(pf)
    session.flush()
    return tenant, pf


def _add_account(session, tenant, pf, broker: str, external_id: str, *, cash_balance_usd=None) -> models.Account:
    acct = models.Account(
        tenant_id=tenant.id, portfolio_id=pf.id, broker=broker,
        external_id=external_id, currency="USD", cash_balance_usd=cash_balance_usd,
    )
    session.add(acct)
    session.flush()
    return acct


def _add_txn(session, tenant, acct, sec, *, txn_type: str, qty: float = 0.0, price: float = 0.0, amount: float = 0.0, when: date, key: str):
    session.add(
        models.Transaction(
            tenant_id=tenant.id, account_id=acct.id, security_id=sec.id if sec else None,
            txn_type=txn_type, quantity=qty, price=price, amount=amount or (qty * price),
            currency="USD", trade_date=when, source_key=key,
        )
    )


class TestSnapshotSourcedCash:
    def test_persisted_cash_balance_surfaces_on_accounts(self, db_session):
        tenant, pf = _seed_portfolio(db_session)
        _add_account(db_session, tenant, pf, "ibkr_flex", "U1", cash_balance_usd=20_300.0)
        db_session.commit()

        infos = analytics.accounts(db_session, tenant.id, pf.id)
        assert len(infos) == 1
        assert infos[0].cash == 20_300.0

    def test_never_synced_account_reports_unknown_not_zero(self, db_session):
        """A snapshot-sourced account that predates this column (or hasn't re-synced
        since) has ``cash_balance_usd IS NULL`` — must surface as None (unknown),
        never silently coerced to a fabricated 0.0."""
        tenant, pf = _seed_portfolio(db_session)
        _add_account(db_session, tenant, pf, "snaptrade", "ST-1", cash_balance_usd=None)
        db_session.commit()

        infos = analytics.accounts(db_session, tenant.id, pf.id)
        assert infos[0].cash is None

    def test_summary_sums_cash_separately_from_market_value(self, db_session):
        tenant, pf = _seed_portfolio(db_session)
        sec = models.Security(symbol="AMD", currency="USD")
        db_session.add(sec)
        db_session.flush()
        acct = _add_account(db_session, tenant, pf, "reference", "REF-1", cash_balance_usd=20_300.0)
        db_session.add(
            models.Position(
                tenant_id=tenant.id, account_id=acct.id, security_id=sec.id,
                quantity=100, avg_cost=50.0, currency="USD",
                market_price=60.0, market_value_local=6000.0, as_of=date(2026, 6, 18),
            )
        )
        db_session.commit()

        summary = analytics.summary(db_session, tenant.id, pf.id)
        assert summary.market_value == 6000.0  # holdings-only, unaffected by cash
        assert summary.cash == 20_300.0
        assert summary.unrealized_gain == 6000.0 - 5000.0  # cost basis untouched by cash either


class TestLedgerSourcedCash:
    def test_deposit_and_dividend_accrue_ledger_cash(self, db_session):
        """A CSV account has no connector cash balance (``cash_usd`` defaults to 0.0
        at persistence) — its cash comes from the transaction ledger instead."""
        tenant, pf = _seed_portfolio(db_session)
        acct = _add_account(db_session, tenant, pf, "csv", "CSV-1", cash_balance_usd=0.0)
        _add_txn(db_session, tenant, acct, None, txn_type="DEPOSIT", amount=5_000.0, when=date(2024, 1, 1), key="d1")
        _add_txn(db_session, tenant, acct, None, txn_type="DIVIDEND", amount=40.0, when=date(2024, 6, 1), key="dv1")
        _add_txn(db_session, tenant, acct, None, txn_type="WITHDRAWAL", amount=500.0, when=date(2024, 7, 1), key="w1")
        db_session.commit()

        infos = analytics.accounts(db_session, tenant.id, pf.id)
        assert infos[0].cash == 5_000.0 + 40.0 - 500.0

    def test_buy_reduces_ledger_cash(self, db_session):
        tenant, pf = _seed_portfolio(db_session)
        sec = models.Security(symbol="AAPL", currency="USD")
        db_session.add(sec)
        db_session.flush()
        acct = _add_account(db_session, tenant, pf, "csv", "CSV-2")
        _add_txn(db_session, tenant, acct, None, txn_type="DEPOSIT", amount=10_000.0, when=date(2024, 1, 1), key="d1")
        _add_txn(db_session, tenant, acct, sec, txn_type="BUY", qty=10, price=150.0, when=date(2024, 1, 15), key="b1")
        db_session.commit()

        infos = analytics.accounts(db_session, tenant.id, pf.id)
        assert infos[0].cash == 10_000.0 - 1_500.0

    def test_unreplayable_ticker_group_excluded_not_fatal_to_account_cash(self, db_session):
        """A SELL exceeding reconstructable BUYs for ONE ticker (incomplete broker
        activity feed — the live E*TRADE-via-SnapTrade shape, see
        test_partial_history_ledger.py) must not lose the account's ENTIRE cash
        figure — only that ticker's group is skipped (WARN-logged); the account's
        other, healthy cash-only transactions still accrue."""
        tenant, pf = _seed_portfolio(db_session)
        sq = models.Security(symbol="SQ", currency="USD")
        db_session.add(sq)
        db_session.flush()
        acct = _add_account(db_session, tenant, pf, "csv", "CSV-3")
        _add_txn(db_session, tenant, acct, None, txn_type="DEPOSIT", amount=1_000.0, when=date(2024, 1, 1), key="d1")
        # SELL with no prior BUY in the feed for this ticker — build_ledger raises for
        # this (account, ticker) group alone.
        _add_txn(db_session, tenant, acct, sq, txn_type="SELL", qty=27, price=64.0, when=date(2024, 7, 17), key="s1")
        db_session.commit()

        infos = analytics.accounts(db_session, tenant.id, pf.id)
        assert infos[0].cash == 1_000.0  # the deposit still counts; the broken SQ group doesn't crash it


class TestMixedPortfolio:
    def test_summary_cash_sums_across_snapshot_and_ledger_sourced_accounts(self, db_session):
        tenant, pf = _seed_portfolio(db_session)
        _add_account(db_session, tenant, pf, "ibkr_flex", "U1", cash_balance_usd=20_300.0)
        csv_acct = _add_account(db_session, tenant, pf, "csv", "CSV-1")
        _add_txn(db_session, tenant, csv_acct, None, txn_type="DEPOSIT", amount=1_000.0, when=date(2024, 1, 1), key="d1")
        db_session.commit()

        summary = analytics.summary(db_session, tenant.id, pf.id)
        assert summary.cash == 20_300.0 + 1_000.0
