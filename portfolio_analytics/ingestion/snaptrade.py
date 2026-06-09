"""SnapTrade connector — normalizes the SnapTrade reader's output into canonical records.

Wraps an existing ``SnapTradeReader`` (the thin API client) and maps its dicts /
DataFrame into the canonical schema. The reader is injected, so this is unit-testable
with a fake reader (no SnapTrade SDK / network). Reuses the battle-tested SnapTrade
parsing helpers in ``loaders.transactions`` (the nested-symbol/currency extraction)
so the canonicalization can't drift from the existing consumer logic.

Fail-soft: any reader failure returns a snapshot with ``error`` set (empty record
lists) so the ingestion layer degrades to last-good rather than raising.
"""

from __future__ import annotations

import logging
from datetime import datetime

from portfolio_analytics.broker_io.transactions import (
    _TYPE_MAP,
    _extract_currency,
    _extract_ticker,
    _f,
    _parse_date,
)
from portfolio_analytics.domain.ledger import TxnType
from portfolio_analytics.ingestion.base import ConnectorSnapshot
from portfolio_analytics.ingestion.schema import (
    CanonicalAccount,
    CanonicalActivity,
    CanonicalHolding,
    CanonicalSecurity,
    synth_security_id,
)

logger = logging.getLogger(__name__)

SOURCE = "snaptrade"

# SnapTrade activity type → canonical TxnType. Extends the tranche-replay map with
# INTEREST (which the tranche map omits, since interest isn't a lot event) so the
# canonical layer can represent — and preserve — interest income through the round
# trip. Unmapped types are dropped (they were invisible to both consumers already).
_ACTIVITY_TYPE_MAP = {**_TYPE_MAP, "INTEREST": TxnType.INTEREST}


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


class SnapTradeConnector:
    """``BrokerConnector`` over a ``SnapTradeReader`` (or any object exposing
    ``get_accounts`` / ``get_all_holdings`` / ``get_all_activities``)."""

    source = SOURCE

    def __init__(self, reader):
        self._reader = reader

    def sync(self, state: dict | None = None) -> ConnectorSnapshot:
        try:
            accounts_raw = self._reader.get_accounts()
            holdings_df = self._reader.get_all_holdings()
            activities_raw = self._reader.get_all_activities()
        except Exception as e:  # noqa: BLE001 — degrade to last-good, never crash ingest
            logger.warning("SnapTrade sync failed: %s", e)
            return ConnectorSnapshot(source=SOURCE, error=str(e))

        snapshot = ConnectorSnapshot(source=SOURCE)
        seen_securities: dict[str, CanonicalSecurity] = {}

        for acct in accounts_raw:
            snapshot.accounts.append(
                CanonicalAccount(
                    number=acct.get("number", "") or "",
                    institution=acct.get("institution", "") or "",
                    nav_usd=float(acct.get("balance_total", 0.0) or 0.0),
                    as_of=_parse_dt(acct.get("last_holdings_sync")),
                    source=SOURCE,
                    account_id=str(acct.get("id", "") or ""),
                    name=acct.get("name", "") or "",
                    account_type=acct.get("type", "") or "",
                )
            )

        if holdings_df is not None and not holdings_df.empty:
            for row in holdings_df.to_dict(orient="records"):
                ticker = row.get("ticker", "") or ""
                if not ticker:
                    continue
                currency = row.get("currency", "USD") or "USD"
                sid = synth_security_id(ticker, currency)
                seen_securities.setdefault(sid, CanonicalSecurity(security_id=sid, ticker=ticker, currency=currency))
                shares = _f(row.get("shares"))
                avg_cost = _f(row.get("avg_cost"))
                snapshot.holdings.append(
                    CanonicalHolding(
                        account_number=row.get("account_number", "") or "",
                        security_id=sid,
                        quantity=shares,
                        cost_basis=shares * avg_cost,
                        avg_cost=avg_cost,
                        market_value_local=_f(row.get("market_value")),
                        currency=currency,
                        source=SOURCE,
                    )
                )

        for a in activities_raw:
            txn_type = _ACTIVITY_TYPE_MAP.get(str(a.get("type", "")).upper().strip())
            if txn_type is None:
                continue  # unmapped type — invisible to both consumers, drop
            when = _parse_date(a.get("trade_date") or a.get("settlement_date"))
            if when is None:
                continue
            ticker = _extract_ticker(a)
            currency = _extract_currency(a)
            sid = synth_security_id(ticker, currency) if ticker else ""
            if sid:
                seen_securities.setdefault(sid, CanonicalSecurity(security_id=sid, ticker=ticker, currency=currency))
            snapshot.activities.append(
                CanonicalActivity(
                    account_number=a.get("account_number", "") or "",
                    when=when,
                    type=txn_type,
                    security_id=sid,
                    quantity=abs(_f(a.get("units"))),
                    price=_f(a.get("price")),
                    amount=abs(_f(a.get("amount"))),
                    fees=abs(_f(a.get("fee"))),
                    currency=currency,
                    source=SOURCE,
                )
            )

        snapshot.securities = list(seen_securities.values())
        return snapshot
