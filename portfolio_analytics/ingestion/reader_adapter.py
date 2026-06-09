"""Read adapter — re-exposes the ``SnapTradeReader`` surface over the canonical store.

This is the drop-in the dashboard reads when the connector layer is enabled: it
implements the exact methods + dict/DataFrame shapes the consumers expect
(``get_accounts`` / ``get_all_holdings`` / ``get_aggregated_holdings`` /
``get_all_activities`` / ``get_total_nav``), but sourced from the merged silver
store instead of a single broker API. Consumers (``load_portfolio``,
``account_breakdown``, ``build_realized_income``, ``reconstruct_tranches``,
``views/preferences``) are unchanged — the adapter reproduces SnapTrade's shapes,
including round-tripping canonical activities back to the activity-dict form the
tranche/realized loaders parse.

Retiring that activity round-trip (and reading canonical activities directly) is a
deliberate post-cutover cleanup — keeping the consumers untouched here is what makes
the connector layer a revertible config flip.
"""

from __future__ import annotations

import pandas as pd

from portfolio_analytics.broker_io.snaptrade_reader import aggregate_holdings
from portfolio_analytics.ingestion.store import CanonicalStore


class CanonicalReader:
    """Reader-shaped view over a ``CanonicalStore``. Drop-in for ``SnapTradeReader``."""

    def __init__(self, store: CanonicalStore):
        self._store = store

    def get_accounts(self) -> list[dict]:
        out = []
        for a in self._store.all_accounts():
            out.append(
                {
                    "id": a.account_id or a.number,
                    "name": a.name,
                    "number": a.number,
                    "type": a.account_type,
                    "balance_total": a.nav_usd,
                    "last_holdings_sync": a.as_of.isoformat() if a.as_of else None,
                    "institution": a.institution,
                }
            )
        return out

    def get_all_holdings(self) -> pd.DataFrame:
        rows = []
        for h in self._store.all_holdings():
            sec = self._store.security(h.security_id)
            acct = self._store.accounts.get(h.account_number)
            rows.append(
                {
                    "account_id": (acct.account_id or acct.number) if acct else h.account_number,
                    "ticker": sec.ticker if sec else "",
                    "currency": h.currency,
                    "shares": h.quantity,
                    "avg_cost": h.avg_cost,
                    # units × native price; recovered from MV/qty (MV == units × price).
                    "current_price": (h.market_value_local / h.quantity) if h.quantity else 0.0,
                    "market_value": h.market_value_local,
                    "account_name": acct.name if acct else "",
                    "account_number": h.account_number,
                    "account_type": acct.account_type if acct else "",
                }
            )
        return pd.DataFrame(rows)

    def get_aggregated_holdings(self, account_numbers: list[str] | None = None) -> pd.DataFrame:
        return aggregate_holdings(self.get_all_holdings(), account_numbers)

    def get_all_activities(self) -> list[dict]:
        """Round-trip canonical activities to the SnapTrade activity-dict shape the
        tranche/realized loaders parse (``type``, ``trade_date``, ``units``,
        ``price``, ``amount``, ``fee``, ``account_number``, nested ``symbol``)."""
        out = []
        for act in self._store.all_activities():
            sec = self._store.security(act.security_id)
            ticker = sec.ticker if sec else ""
            out.append(
                {
                    "type": str(act.type),
                    "trade_date": act.when.isoformat(),
                    "units": act.quantity,
                    "price": act.price,
                    "amount": act.amount,
                    "fee": act.fees,
                    "account_number": act.account_number,
                    "symbol": {"symbol": ticker, "currency": {"code": act.currency}},
                    "currency": {"code": act.currency},
                }
            )
        return out

    def get_total_nav(self) -> float:
        return sum(a.nav_usd for a in self._store.all_accounts())
