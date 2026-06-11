"""Read-only SnapTrade client.

Fetches account data, positions, and balances from linked brokerage accounts
via the SnapTrade API. This module has NO TRADING METHODS — it is read-only
by design.

Usage:
    reader = SnapTradeReader.from_env()
    accounts = reader.get_accounts()
    holdings = reader.get_aggregated_holdings()
    nav = reader.get_total_nav()
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path

import pandas as pd
from snaptrade_client import SnapTrade

logger = logging.getLogger(__name__)

POSITIONS_CACHE_PATH = Path("cache/positions_latest.json")
ACTIVITIES_CACHE_PATH = Path("cache/activities_latest.json")
_ACTIVITIES_PAGE = 1000  # SnapTrade activities page size for offset/limit paging


def aggregate_holdings(df: pd.DataFrame, account_numbers: list[str] | None = None) -> pd.DataFrame:
    """Aggregate per-(account, ticker) holdings to per-ticker rows.

    Args:
        df: Per-account holdings (cols: ticker, currency, shares, avg_cost,
            market_value, account_id, account_number).
        account_numbers: If given, only these accounts are included before
            aggregating (enables per-account / multi-account views). None = all.

    Returns one row per ticker with summed shares/market_value, share-weighted
    avg_cost, and n_accounts. Currency is constant per ticker.
    """
    if df.empty:
        return df
    df = df.copy()
    # Defensive defaults for older/cached frames missing these columns.
    if "currency" not in df.columns:
        df["currency"] = "USD"
    if "account_id" not in df.columns:
        df["account_id"] = df["account_number"] if "account_number" in df.columns else df.index
    if account_numbers is not None and "account_number" in df.columns:
        df = df[df["account_number"].isin(account_numbers)]
    if df.empty:
        return df.iloc[0:0]
    agg = (
        df.groupby("ticker")
        .agg(
            currency=("currency", "first"),
            shares=("shares", "sum"),
            total_cost=("avg_cost", lambda x: (x * df.loc[x.index, "shares"]).sum()),
            market_value=("market_value", "sum"),
            n_accounts=("account_id", "nunique"),
        )
        .reset_index()
    )
    agg["avg_cost"] = (agg["total_cost"] / agg["shares"]).round(4)
    agg.drop(columns=["total_cost"], inplace=True)
    return agg


class SnapTradeReader:
    """Read-only SnapTrade client. NO TRADING METHODS."""

    def __init__(self, client_id: str, consumer_key: str, user_id: str, user_secret: str):
        self._client = SnapTrade(consumer_key=consumer_key, client_id=client_id)
        self._user_id = user_id
        self._user_secret = user_secret

    @classmethod
    def from_env(cls) -> SnapTradeReader:
        """Create reader from environment variables."""
        return cls(
            client_id=os.environ["SNAPTRADE_CLIENT_ID"],
            consumer_key=os.environ["SNAPTRADE_CONSUMER_KEY"],
            user_id=os.environ["SNAPTRADE_USER_ID"],
            user_secret=os.environ["SNAPTRADE_USER_SECRET"],
        )

    def get_accounts(self) -> list[dict]:
        """Return all linked accounts with id, name, type, and institution."""
        response = self._client.account_information.list_user_accounts(
            user_id=self._user_id,
            user_secret=self._user_secret,
        )
        accounts = []
        for acct in response.body:
            # IBKR's own total account value in USD (positions + cash, converted
            # with IBKR's FX). This is the authoritative NAV per account — we
            # derive cash from it rather than summing per-currency cash buckets,
            # which would otherwise add HKD/SGD/USD at face value.
            balance = acct.get("balance") or {}
            total = balance.get("total") or {} if isinstance(balance, dict) else {}
            balance_total = float(total.get("amount", 0.0)) if isinstance(total, dict) else 0.0
            # Holdings sync timestamp — SnapTrade refreshes a Daily-plan account's
            # holdings ~once/day at a per-brokerage time, so this is how stale the
            # positions/cash for THIS account actually are (often hours old).
            sync = acct.get("sync_status") or {}
            holdings_sync = (sync.get("holdings") or {}) if isinstance(sync, dict) else {}
            last_sync = holdings_sync.get("last_successful_sync") if isinstance(holdings_sync, dict) else None
            accounts.append(
                {
                    "id": str(acct.get("id", "")),
                    "name": acct.get("name", ""),
                    "number": acct.get("number", ""),
                    "type": acct.get("institution_type", acct.get("type", "")),
                    "balance_total": balance_total,
                    "last_holdings_sync": last_sync,
                    # SnapTrade's brokerage name lives in the top-level
                    # ``institution_name`` string (e.g. "Fidelity",
                    # "Interactive Brokers"). ``brokerage_authorization`` is a
                    # bare authorization UUID, NOT a nested brokerage object —
                    # the old ``brokerage_authorization.brokerage.name`` path
                    # never matched and left institution blank for every account.
                    "institution": acct.get("institution_name", "") or "",
                    # Bare authorization UUID linking the account to its brokerage
                    # connection (see ``get_connections``).
                    "brokerage_authorization": str(acct.get("brokerage_authorization", "") or ""),
                }
            )
        logger.info("Found %d linked accounts", len(accounts))
        return accounts

    def get_connections(self) -> list[dict]:
        """Return linked brokerage connections (authorizations): id, brokerage, disabled.

        A disabled connection needs a reconnect through the connection portal
        (``get_login_url``) — its accounts stop refreshing until repaired.
        """
        response = self._client.connections.list_brokerage_authorizations(
            user_id=self._user_id,
            user_secret=self._user_secret,
        )
        connections = []
        for auth in response.body:
            brokerage = auth.get("brokerage") or {}
            name = ""
            if isinstance(brokerage, dict):
                name = brokerage.get("display_name") or brokerage.get("name") or ""
            connections.append(
                {
                    "id": str(auth.get("id", "")),
                    "brokerage": str(name or auth.get("name", "") or ""),
                    "disabled": bool(auth.get("disabled") or False),
                }
            )
        logger.info("Found %d brokerage connections", len(connections))
        return connections

    def get_login_url(self, broker: str | None = None) -> str:
        """Return a short-lived SnapTrade connection-portal URL for this user.

        The portal is where a NEW brokerage gets linked (E*TRADE, Schwab, …) or a
        broken connection repaired — SnapTrade hosts the brokerage login itself.
        ``connection_type="read"`` keeps every connection read-only: this module has
        NO TRADING METHODS and the portal link must match that posture.
        """
        kwargs: dict = {
            "user_id": self._user_id,
            "user_secret": self._user_secret,
            "connection_type": "read",
        }
        if broker:
            kwargs["broker"] = broker
        response = self._client.authentication.login_snap_trade_user(**kwargs)
        body = response.body or {}
        url = body.get("redirectURI") or body.get("loginRedirectURI") or ""
        if not url:
            raise RuntimeError("SnapTrade did not return a connection-portal URL")
        return str(url)

    def get_holdings(self, account_id: str) -> list[dict]:
        """Get positions for a single account."""
        response = self._client.account_information.get_user_holdings(
            account_id=account_id,
            user_id=self._user_id,
            user_secret=self._user_secret,
        )
        holdings = []
        for pos in response.body.get("positions", []):
            symbol_info = pos.get("symbol", {}) or {}
            symbol_obj = symbol_info.get("symbol", {}) or {}
            ticker = symbol_obj.get("symbol", "") if isinstance(symbol_obj, dict) else str(symbol_obj)
            if not ticker:
                continue
            # Native trading currency (e.g. SGD for SGX, HKD for SEHK). Prefer the
            # symbol's currency; fall back to the position-level currency, then USD.
            ccy_obj = (symbol_obj.get("currency") if isinstance(symbol_obj, dict) else None) or pos.get("currency")
            currency = ccy_obj.get("code", "USD") if isinstance(ccy_obj, dict) else "USD"
            price = float(pos.get("price") or 0)
            # Some brokers report no cost basis at all — e.g. Fidelity for a
            # 401(k) plan-level CIT like the TRP Retirement Blend target-date
            # trust (ticker PCKM), where ``average_purchase_price`` is null.
            # These are tax-advantaged accounts where cost basis is meaningless;
            # fall back to the current price so cost basis equals market value
            # (0 unrealized P&L). Treating an unknown cost as $0 would instead
            # report the entire position as phantom unrealized gain and inflate
            # portfolio-wide P&L. Check ``is None`` explicitly — a genuine 0
            # avg cost never occurs, but we don't want to swallow a real value.
            avg_purchase = pos.get("average_purchase_price")
            avg_cost = float(avg_purchase) if avg_purchase is not None else price
            holdings.append(
                {
                    "account_id": account_id,
                    "ticker": ticker,
                    "currency": currency,
                    "shares": float(pos.get("units", 0)),
                    "avg_cost": avg_cost,
                    "current_price": price,
                    # Current market value in the security's native currency.
                    # units × price — NOT open_pnl + units × avg_cost: SnapTrade's
                    # open_pnl can be mis-scaled (observed ~1.6 on a ~1,900 HKD
                    # gain), which silently understated foreign-position value.
                    "market_value": float(pos.get("units", 0)) * price,
                }
            )
        return holdings

    def get_all_holdings(self) -> pd.DataFrame:
        """Get all positions across all accounts."""
        accounts = self.get_accounts()
        all_holdings = []
        for acct in accounts:
            try:
                holdings = self.get_holdings(acct["id"])
                for h in holdings:
                    h["account_name"] = acct["name"]
                    h["account_number"] = acct.get("number", "")
                    h["account_type"] = acct["type"]
                all_holdings.extend(holdings)
            except Exception as e:
                logger.warning("Failed to fetch holdings for account %s: %s", acct["name"], e)
        df = pd.DataFrame(all_holdings)
        if not df.empty:
            self._save_cache(df)
        return df

    def get_aggregated_holdings(self, account_numbers: list[str] | None = None) -> pd.DataFrame:
        """Get holdings aggregated by ticker, optionally for a subset of accounts.

        Computes weighted average cost basis across the selected sub-accounts.
        ``account_numbers=None`` aggregates all accounts (consolidated view).
        """
        return aggregate_holdings(self.get_all_holdings(), account_numbers)

    def get_account_activities(self, account_id: str, start_date=None) -> list[dict]:
        """Get the transaction/activity history for a single account.

        Paginates the SnapTrade ``/accounts/{id}/activities`` endpoint (BUY / SELL
        / DIVIDEND / transfers / fees) and returns the raw activity dicts. This is
        the ONLY source of per-lot detail — the holdings endpoint exposes just one
        aggregate ``average_purchase_price``. History depth is whatever the broker
        (IBKR via SnapTrade) reports; lots opened before that window are absent.

        ``start_date`` (a ``datetime.date``) optionally bounds the lookback.
        """
        out: list[dict] = []
        offset = 0
        while True:
            response = self._client.account_information.get_account_activities(
                account_id=account_id,
                user_id=self._user_id,
                user_secret=self._user_secret,
                offset=offset,
                limit=_ACTIVITIES_PAGE,
                **({"start_date": start_date} if start_date else {}),
            )
            # The endpoint may return a bare list or a paginated envelope with a
            # ``data`` list — handle both. frozendict bodies are dict-like.
            body = response.body
            page = body.get("data", []) if isinstance(body, dict) else list(body)
            out.extend(dict(a) for a in page)
            if len(page) < _ACTIVITIES_PAGE:
                break
            offset += _ACTIVITIES_PAGE
        return out

    def get_all_activities(self) -> list[dict]:
        """Get activities across all accounts, each tagged with ``account_number``.

        Best-effort per account (a single account's failure is logged and skipped,
        mirroring ``get_all_holdings``). Caches the merged result for offline use.
        """
        all_activities: list[dict] = []
        for acct in self.get_accounts():
            try:
                activities = self.get_account_activities(acct["id"])
            except Exception as e:
                logger.warning("Failed to fetch activities for account %s: %s", acct["name"], e)
                continue
            for a in activities:
                a["account_number"] = acct.get("number", "")
            all_activities.extend(activities)
        if all_activities:
            self._save_activities_cache(all_activities)
        return all_activities

    def get_total_nav(self) -> float:
        """Get total net asset value across all accounts.

        Uses IBKR's authoritative per-account ``balance_total`` (USD, positions +
        cash, converted with IBKR's own FX). We do NOT sum per-currency cash
        buckets ourselves — the SnapTrade balance endpoint returns one ``cash``
        entry per currency, and adding HKD/SGD/USD at face value overstates NAV.
        """
        return sum(float(a.get("balance_total", 0.0)) for a in self.get_accounts())

    def _save_cache(self, df: pd.DataFrame) -> None:
        """Save positions to local cache for offline fallback."""
        try:
            POSITIONS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            cache_data = {
                "timestamp": datetime.now().isoformat(),
                "positions": df.to_dict(orient="records"),
            }
            POSITIONS_CACHE_PATH.write_text(json.dumps(cache_data, indent=2, default=str))
        except Exception as e:
            logger.debug("Failed to save positions cache: %s", e)

    @staticmethod
    def load_cached_holdings() -> tuple[pd.DataFrame, str]:
        """Load last-known positions from cache. Returns (df, timestamp)."""
        if not POSITIONS_CACHE_PATH.exists():
            return pd.DataFrame(), ""
        data = json.loads(POSITIONS_CACHE_PATH.read_text())
        return pd.DataFrame(data["positions"]), data["timestamp"]

    def _save_activities_cache(self, activities: list[dict]) -> None:
        """Save activities to local cache for offline fallback."""
        try:
            ACTIVITIES_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            cache_data = {"timestamp": datetime.now().isoformat(), "activities": activities}
            ACTIVITIES_CACHE_PATH.write_text(json.dumps(cache_data, indent=2, default=str))
        except Exception as e:
            logger.debug("Failed to save activities cache: %s", e)

    @staticmethod
    def load_cached_activities() -> tuple[list[dict], str]:
        """Load last-known activities from cache. Returns (activities, timestamp)."""
        if not ACTIVITIES_CACHE_PATH.exists():
            return [], ""
        data = json.loads(ACTIVITIES_CACHE_PATH.read_text())
        return data.get("activities", []), data.get("timestamp", "")
