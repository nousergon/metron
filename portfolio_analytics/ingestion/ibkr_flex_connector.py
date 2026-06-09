"""IBKR Flex connector — a full canonical snapshot from one Activity Flex Query.

SnapTrade exposes no IBKR activity and had FX/cash bugs, so IBKR's own Flex Query is
the authoritative source. This connector fetches one multi-section Activity Flex
statement and normalizes every section into the canonical schema:

  * **EquitySummaryInBase** + **AccountInformation** → ``CanonicalAccount``
    (authoritative base-currency NAV + cash, account name/type)
  * **OpenPositions** → ``CanonicalHolding`` + ``CanonicalSecurity`` (native units,
    cost basis, mark price)
  * **CashTransactions** → ``CanonicalActivity`` (dividends / interest / fees)
  * **Trades @ Closed Lots** → ``RealizedGain`` (reuses the verified
    ``loaders.ibkr_flex.parse_realized_lots``)

The element/attribute names follow IBKR's documented Flex XML schema. Only the
``<Lot>`` (realized) section has been verified against a live statement; the
positions/cash/equity parsers are validated against a fixture here and must be
reconciled against the live re-scoped query before the IBKR cutover (PR4). Fail-soft:
any fetch error returns a snapshot with ``error`` set so ingest keeps last-good.
"""

from __future__ import annotations

import logging
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime

from portfolio_analytics.broker_io.flex_xml import _f, _parse_flex_date, _tag, fetch_flex_xml, parse_realized_lots
from portfolio_analytics.domain.ledger import TxnType
from portfolio_analytics.ingestion.base import ConnectorSnapshot
from portfolio_analytics.ingestion.schema import (
    ASSET_EQUITY,
    ASSET_ETF,
    ASSET_FUND,
    ASSET_OTHER,
    CanonicalAccount,
    CanonicalActivity,
    CanonicalHolding,
    CanonicalSecurity,
    synth_security_id,
)
from portfolio_analytics.ingestion.store import save_bronze

logger = logging.getLogger(__name__)

SOURCE = "ibkr_flex"

# Position asset categories we don't surface as holdings. CASH isn't a position;
# OPT (options) carry a ×100 multiplier + expiry and yfinance can't price them — v1
# skips them rather than mis-value. (Revisit: carry OPT with broker MV, no yfinance.)
_SKIP_POSITION_CATEGORIES = {"CASH", "OPT"}

_ASSET_CATEGORY_MAP = {"STK": ASSET_EQUITY, "ETF": ASSET_ETF, "FUND": ASSET_FUND, "MF": ASSET_FUND}


def _asset_type(category: str) -> str:
    return _ASSET_CATEGORY_MAP.get((category or "").upper(), ASSET_OTHER)


def _as_dt(value: str | None) -> datetime | None:
    d = _parse_flex_date(value)
    return datetime(d.year, d.month, d.day) if d else None


def _map_cash_type(type_str: str, amount: float) -> TxnType | None:
    """Map an IBKR CashTransaction ``type`` to a canonical ``TxnType``.

    Interest *paid* and withholding/fees are expenses (→ FEE), interest *received*
    is income (→ INTEREST), so interest income isn't overstated. Deposits/Withdrawals
    is a single signed IBKR type → split by sign. Unmapped → None (dropped)."""
    u = (type_str or "").upper()
    if "INTEREST" in u:
        return TxnType.FEE if "PAID" in u else TxnType.INTEREST
    if "DIVIDEND" in u or "PAYMENT IN LIEU" in u:
        return TxnType.DIVIDEND
    if "WITHHOLDING" in u or "TAX" in u or "FEE" in u or "COMMISSION" in u:
        return TxnType.FEE
    if "DEPOSIT" in u or "WITHDRAWAL" in u:
        return TxnType.DEPOSIT if amount >= 0 else TxnType.WITHDRAWAL
    return None


def parse_accounts(root: ET.Element) -> dict[str, dict]:
    """Merge AccountInformation (name/type) + EquitySummary (NAV/cash) per accountId.

    NAV/cash come from EquitySummaryByReportDateInBase (already FX-converted to base
    — the analog of SnapTrade's ``balance_total``; never sum per-currency cash). The
    latest reportDate wins."""
    accts: dict[str, dict] = {}
    for e in root.iter():
        tag = _tag(e)
        aid = e.get("accountId", "") or ""
        if not aid:
            continue
        if tag == "AccountInformation":
            a = accts.setdefault(aid, {})
            a["name"] = e.get("acctAlias") or e.get("name") or ""
            a["account_type"] = e.get("accountType", "") or ""
            a["currency"] = e.get("currency", "USD") or "USD"
        elif tag == "EquitySummaryByReportDateInBase":
            report_date = e.get("reportDate", "") or ""
            a = accts.setdefault(aid, {})
            if report_date >= a.get("_report_date", ""):
                a["_report_date"] = report_date
                a["nav"] = _f(e.get("total"))
                a["cash"] = _f(e.get("cash"))
                a["as_of"] = _as_dt(report_date)
    return accts


def parse_open_positions(root: ET.Element):
    """Yield ``(CanonicalHolding, CanonicalSecurity)`` for each open equity position."""
    out = []
    for e in root.iter():
        if _tag(e) != "OpenPosition":
            continue
        # When the query's Open Positions detail = Lot, IBKR emits both per-LOT rows
        # AND a SUMMARY rollup per position — counting both would double the position.
        # Keep only the SUMMARY (or unlabeled) rows. (Live is SUMMARY-only today.)
        lod = (e.get("levelOfDetail") or "").upper()
        if lod and lod != "SUMMARY":
            continue
        if (e.get("assetCategory") or "").upper() in _SKIP_POSITION_CATEGORIES:
            continue
        ticker = e.get("symbol", "") or ""
        if not ticker:
            continue
        currency = e.get("currency", "USD") or "USD"
        sid = synth_security_id(ticker, currency)
        qty = _f(e.get("position"))
        avg_cost = _f(e.get("costBasisPrice"))
        # Market value: prefer the native positionValue; else position × markPrice.
        mv = _f(e.get("positionValue")) or (qty * _f(e.get("markPrice")))
        holding = CanonicalHolding(
            account_number=e.get("accountId", "") or "",
            security_id=sid,
            quantity=qty,
            cost_basis=abs(_f(e.get("costBasisMoney"))) or abs(qty * avg_cost),
            avg_cost=avg_cost,
            market_value_local=mv,
            currency=currency,
            source=SOURCE,
        )
        sec = CanonicalSecurity(
            security_id=sid, ticker=ticker, currency=currency, asset_type=_asset_type(e.get("assetCategory", ""))
        )
        out.append((holding, sec))
    return out


def parse_cash_transactions(root: ET.Element):
    """Yield ``(CanonicalActivity, CanonicalSecurity | None)`` for cash transactions."""
    out = []
    for e in root.iter():
        if _tag(e) != "CashTransaction":
            continue
        amount = _f(e.get("amount"))
        ttype = _map_cash_type(e.get("type", ""), amount)
        if ttype is None:
            continue
        when = _parse_flex_date(e.get("dateTime") or e.get("settleDate") or e.get("reportDate"))
        if when is None:
            continue
        currency = e.get("currency", "USD") or "USD"
        ticker = e.get("symbol", "") or ""
        sid = synth_security_id(ticker, currency) if ticker else ""
        sec = CanonicalSecurity(security_id=sid, ticker=ticker, currency=currency) if sid else None
        out.append(
            (
                CanonicalActivity(
                    account_number=e.get("accountId", "") or "",
                    when=when,
                    type=ttype,
                    security_id=sid,
                    amount=abs(amount),
                    currency=currency,
                    source=SOURCE,
                ),
                sec,
            )
        )
    return out


class IbkrFlexConnector:
    """``BrokerConnector`` over an IBKR Activity Flex Query (all sections)."""

    source = SOURCE

    def __init__(self, token: str, query_id: str, *, opener=urllib.request.urlopen, fetcher=None):
        self._token = token
        self._query_id = query_id
        # ``fetcher`` is injectable for tests (returns the statement XML directly).
        self._fetcher = fetcher or (lambda: fetch_flex_xml(token, query_id, opener=opener))

    def sync(self, state: dict | None = None) -> ConnectorSnapshot:
        try:
            xml = self._fetcher()
        except Exception as e:  # noqa: BLE001 — degrade to last-good, never crash ingest
            logger.warning("IBKR Flex sync failed: %s", e)
            return ConnectorSnapshot(source=SOURCE, error=str(e))

        save_bronze(SOURCE, xml)  # land the raw statement for replay/audit
        try:
            root = ET.fromstring(xml)
        except ET.ParseError as e:
            return ConnectorSnapshot(source=SOURCE, error=f"unparseable Flex XML: {e}")

        snapshot = ConnectorSnapshot(source=SOURCE)
        securities: dict[str, CanonicalSecurity] = {}

        acct_meta = parse_accounts(root)
        for number, m in acct_meta.items():
            snapshot.accounts.append(
                CanonicalAccount(
                    number=number,
                    institution="Interactive Brokers",
                    nav_usd=m.get("nav", 0.0),
                    cash_usd=m.get("cash", 0.0),
                    currency="USD",
                    as_of=m.get("as_of"),
                    source=SOURCE,
                    account_id=number,  # Flex has no separate id — the number is the id
                    name=m.get("name", ""),
                    account_type=m.get("account_type", ""),
                )
            )

        for holding, sec in parse_open_positions(root):
            snapshot.holdings.append(holding)
            securities.setdefault(sec.security_id, sec)

        for activity, sec in parse_cash_transactions(root):
            snapshot.activities.append(activity)
            if sec is not None:
                securities.setdefault(sec.security_id, sec)

        snapshot.realized_lots = parse_realized_lots(xml)
        for _number, rg in snapshot.realized_lots:
            sid = synth_security_id(rg.ticker)
            securities.setdefault(sid, CanonicalSecurity(security_id=sid, ticker=rg.ticker))

        snapshot.securities = list(securities.values())
        return snapshot
