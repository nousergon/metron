"""Portfolio analytics over persisted transactions.

Reads a portfolio's stored ``transactions`` and runs them through the engine ledger
(``portfolio_analytics.domain.ledger``) to derive **current holdings** (FIFO cost
basis) and **realized gains** (short/long-term) — the price-free analytics that a
plain transaction history fully determines.

Market value, unrealized P&L, and time-weighted performance need an EOD price series
and are intentionally out of scope here: they arrive with the price service (plan §6
PH1 Marketstack increment). Reporting a market value we cannot source would violate
the product's no-fabrication posture, so this layer returns only what the ledger
proves.

Holdings are derived live from the ledger rather than read from the ``positions``
table: for a CSV/transaction source the ledger IS the position truth, and deriving
keeps the holding reconciled to the transaction history by construction.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from collections.abc import Collection
from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.db import models
from api.services import fx as fx_service
from api.services import labels
from api.services import prices as price_service
from portfolio_analytics.domain.ledger import Ledger, RealizedGain, Transaction, TxnType, build_ledger
from portfolio_analytics.domain.realized import YearlyIncome, summarize_income_by_year
from portfolio_analytics.ingestion.base import SNAPSHOT_SOURCES
from portfolio_analytics.prices import ClosePoint

logger = logging.getLogger(__name__)


@dataclass
class Holding:
    ticker: str
    quantity: float
    avg_cost: float       # native per-share cost
    cost_basis: float     # native total cost basis (price-free; always known)
    currency: str = "USD"  # instrument's native currency (ISO-4217)
    # Broker-reported native price/value (snapshot sources) — the valuation fallback
    # when the price cache can't resolve a foreign listing. None for ledger-only holdings.
    broker_market_price: float | None = None
    broker_as_of: date | None = None
    # Valuation — populated by valued_holdings. ``_local`` fields are in the holding's
    # native currency; the bare fields are converted to the portfolio BASE currency.
    # A foreign holding with no cached FX rate keeps the base fields None (never
    # fabricates 1 HKD = 1 USD) while still showing its native ``_local`` values.
    fx_rate: float | None = None              # base per 1 unit of `currency` (1.0 for USD)
    last_price: float | None = None           # native last price
    last_price_date: date | None = None
    # True when last_price came from the live EOD close feed (cached close), False when it
    # fell back to a broker statement snapshot. Drives the close-feed staleness check —
    # a broker snapshot is legitimately old and must NOT read as a stalled live feed.
    last_price_from_close: bool = False
    market_value_local: float | None = None   # native market value
    cost_basis_base: float | None = None       # cost_basis converted to base
    market_value: float | None = None          # base market value
    unrealized_gain: float | None = None        # base unrealized P&L
    unrealized_pct: float | None = None         # currency-invariant ratio
    # Asset class for grouping (cash / bond / equity / etf / fund / option / other),
    # from the Security master's asset_class with a CUSIP/name fallback (metron-ops#47).
    security_type: str = "other"
    # Account attribution — populated ONLY on the per-account ("uncombined") holdings path
    # (valued_holdings_by_account_flat, metron-ops#114), where one row is one (account,
    # ticker). None on the default consolidated path (one row per ticker across accounts).
    account_id: uuid.UUID | None = None
    account_label: str | None = None  # nickname / name / external_id, resolved once
    # User-set display label/alias (so a numeric-CUSIP bond is legible). None when unset.
    user_label: str | None = None
    # Per-security period returns — populated only by the Holdings endpoint via
    # security_perf.enrich_holdings (None elsewhere). Day legs need the intraday feed;
    # YTD/LTM come from cached daily closes (metron-ops#87).
    overnight_pct: float | None = None
    intraday_pct: float | None = None
    day_pct: float | None = None
    ytd_pct: float | None = None
    ltm_pct: float | None = None
    # True when the close-fed last_price is ≥1 full trading session behind the latest
    # session that should have printed — i.e. the upstream EOD feed has stalled. Stamped
    # by security_perf.enrich_holdings (Holdings view only); the UI surfaces it loudly so
    # a frozen feed never masquerades as a current price.
    last_price_stale: bool = False
    # Reference classification — populated only by the Holdings endpoint via
    # security_perf.enrich_holdings (None elsewhere). GICS sector + country of domicile,
    # cached on the global Security row, sourced from the data spine (metron-ops#…).
    # Country drives the US-vs-international split; both stay None when unclassified
    # (a coverage gap, never a guessed value).
    sector: str | None = None
    country: str | None = None
    # Per-holding valuation / fundamentals / technicals metrics for the Holdings table
    # (Holdings metrics). Populated ONLY by the Holdings endpoint on a feed-entitled build
    # (yfinance-derived data spine → licensed); None off-feed or on a coverage gap, never
    # fabricated. Sourced from the fundamentals + technicals spine artifacts.
    # Valuation:
    market_cap: float | None = None
    pe: float | None = None          # trailing P/E
    fwd_pe: float | None = None
    pb: float | None = None          # price / book
    ps: float | None = None          # price / sales (TTM)
    ev_ebitda: float | None = None
    peg: float | None = None
    div_yield: float | None = None   # fraction
    # Fundamentals:
    rev_growth: float | None = None       # fraction
    earnings_growth: float | None = None  # fraction
    gross_margin: float | None = None     # fraction
    op_margin: float | None = None        # fraction
    roe: float | None = None              # fraction
    roa: float | None = None              # fraction
    beta: float | None = None
    # Balance sheet (absolute $ + leverage/liquidity):
    cash: float | None = None             # total cash ($)
    debt: float | None = None             # total debt ($)
    net_debt: float | None = None         # debt − cash ($); derived
    debt_to_equity: float | None = None   # yfinance raw (a percentage, e.g. 47.2)
    net_debt_to_ebitda: float | None = None  # (debt − cash) / EBITDA; derived leverage
    current_ratio: float | None = None
    quick_ratio: float | None = None
    fcf: float | None = None              # free cash flow ($)
    # Technicals:
    rsi_14: float | None = None
    macd_hist: float | None = None
    pct_to_ma_50: float | None = None     # fraction
    pct_to_ma_200: float | None = None    # fraction
    pct_in_52w_range: float | None = None  # 0-1
    mom_20d: float | None = None          # fraction
    # Consensus research + news sentiment (metron-ops#105, Phase 1). Populated ONLY by the
    # Holdings endpoint on a feed-entitled build (free sources, but licensing-uniform with
    # the rest of the spine → feed-gated); None off-feed or on a coverage gap, never
    # fabricated. Sourced from the analyst + sentiment spine artifacts.
    consensus_rating: str | None = None       # strongBuy/buy/hold/sell/strongSell
    consensus_score: float | None = None      # signed [-1, +1] (strongBuy=+1 … strongSell=-1)
    price_target_mean: float | None = None    # mean analyst target (native price units)
    price_target_median: float | None = None
    price_target_upside: float | None = None  # mean_target / last_price − 1 (fraction); derived
    num_analysts: int | None = None
    news_sentiment: float | None = None       # trust-weighted LM composite ∈ [-1, +1]
    news_articles: int | None = None          # # articles behind the sentiment
    # Composite attractiveness score (metron-ops#106, Phase 2) — a transparent 0–100 blend of
    # the fields above (fwd-P/E vs sector median, upside, rating, revision, sentiment). Set by
    # the Holdings endpoint on a feed-entitled build via api.services.attractiveness; None
    # off-feed or when no component is present, never fabricated.
    attractiveness: float | None = None
    attractiveness_coverage: int | None = None  # # of components that contributed to the score


@dataclass
class RealizedLot:
    ticker: str
    open_date: date
    close_date: date
    quantity: float
    proceeds: float       # native
    cost_basis: float     # native
    gain: float           # native
    long_term: bool
    currency: str = "USD"
    fx_rate: float | None = None     # base per 1 unit of `currency` as of close_date (1.0 for USD)
    # Base-currency conversion at the close-date rate; None when that rate isn't cached
    # (no fabrication — the native gain is still shown).
    gain_base: float | None = None
    proceeds_base: float | None = None
    cost_basis_base: float | None = None


@dataclass
class TransactionRow:
    trade_date: date
    txn_type: str
    ticker: str
    quantity: float
    price: float
    amount: float
    fees: float
    currency: str


def _portfolio_rows(
    session: Session,
    tenant_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    account_id: uuid.UUID | None = None,
    account_ids: Collection[uuid.UUID] | None = None,
):
    """Fetch ``(Transaction, ticker)`` for one portfolio, tenant-scoped, oldest first.

    When ``account_id`` is given the result is narrowed to that single account (the
    per-account drill-down); the caller is responsible for verifying the account
    belongs to the portfolio. ``account_ids`` narrows to a SET of accounts (e.g. the
    taxable subset for the Tax lens) — an empty set yields no rows."""
    stmt = (
        select(models.Transaction, models.Security.symbol)
        .join(models.Account, models.Transaction.account_id == models.Account.id)
        .outerjoin(models.Security, models.Transaction.security_id == models.Security.id)
        .where(
            models.Transaction.tenant_id == tenant_id,
            models.Account.portfolio_id == portfolio_id,
        )
        .order_by(models.Transaction.trade_date, models.Transaction.created_at)
    )
    if account_id is not None:
        stmt = stmt.where(models.Transaction.account_id == account_id)
    if account_ids is not None:
        stmt = stmt.where(models.Transaction.account_id.in_(account_ids))
    return session.execute(stmt).all()


def _normalize_bond_quantity(txn_type: str, quantity: float, price: float, amount: float) -> float:
    r"""Normalize a fixed-income trade's quantity to the per-$100-par unit (metron-ops#74).

    SnapTrade records a bond/CD/treasury BUY/SELL with ``quantity`` = FACE value (e.g.
    10000) but ``price`` = percent of par (e.g. 97.0147, per \$100), so ``quantity*price``
    overstates the cash ~100x — while the broker's POSITION section uses ``quantity`` =
    face/100 (e.g. 100). Replaying the raw transaction therefore inflates the ledger cost
    basis, realized gains, and the reconstructed NAV ~100x (the \$4.5M / −96% Performance
    bug). The ``amount`` field carries the true cash, so the par signature is
    ``quantity*price ≈ 100*amount``; when detected, divide quantity by 100 so
    ``quantity*price ≈ amount`` AND it matches the position unit. Equity trades
    (``quantity*price ≈ amount``) are far outside this band and pass through unchanged."""
    if txn_type in (TxnType.BUY.value, TxnType.SELL.value) and quantity > 0 and price > 0 and amount > 0:
        ratio = (quantity * price) / (100.0 * amount)
        if 0.8 <= ratio <= 1.25:
            return quantity / 100.0
    return quantity


def _to_engine_txn(row: models.Transaction, ticker: str | None) -> Transaction:
    """Map a stored transaction to an engine ``Transaction`` (floats, not Decimal)."""
    quantity = float(row.quantity)
    price = float(row.price)
    amount = float(row.amount)
    quantity = _normalize_bond_quantity(row.txn_type, quantity, price, amount)
    return Transaction(
        when=row.trade_date,
        type=TxnType(row.txn_type),
        ticker=(ticker or "").upper(),
        quantity=quantity,
        price=price,
        amount=amount,
        fees=float(row.fees),
        currency=row.currency,
    )


def engine_transactions(
    session: Session,
    tenant_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    account_id: uuid.UUID | None = None,
    account_ids: Collection[uuid.UUID] | None = None,
) -> list[Transaction]:
    """The portfolio's transactions as engine ``Transaction`` objects, oldest first.

    Exposed for historical reconstruction: replaying ``build_ledger`` over the subset
    with ``when <= d`` gives the positions held as of date ``d``. ``account_ids``
    narrows to a set of accounts (e.g. the taxable subset)."""
    return [txn for _aid, txn in engine_transactions_by_account(session, tenant_id, portfolio_id, account_id, account_ids)]


def engine_transactions_by_account(
    session: Session,
    tenant_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    account_id: uuid.UUID | None = None,
    account_ids: Collection[uuid.UUID] | None = None,
) -> list[tuple[uuid.UUID, Transaction]]:
    """Like ``engine_transactions`` but each transaction is paired with its account id —
    the grouping key ``build_portfolio_ledger`` needs for per-account FIFO lot relief."""
    return [
        (row.account_id, _to_engine_txn(row, ticker))
        for row, ticker in _portfolio_rows(session, tenant_id, portfolio_id, account_id, account_ids)
    ]


@dataclass(frozen=True)
class IncompleteHistory:
    """A per-(account, ticker) transaction group whose history could not be replayed —
    the broker's activity feed starts mid-position (a SELL exceeding reconstructable
    BUYs), so its lots/realized gains are absent from derived analytics."""

    account_id: uuid.UUID | None
    ticker: str
    error: str


def build_portfolio_ledger(
    txns: list[tuple[uuid.UUID | None, Transaction]], *, log: bool = True
) -> tuple[Ledger, list[IncompleteHistory]]:
    """Replay transactions into one merged ``Ledger``, FIFO **per (account, ticker)**.

    Lot relief is per account — a SELL in one account never closes a lot bought in
    another (the IRS reality; mirrors ``reconstruct_tranches``). The strict domain
    ``build_ledger`` raises on a group whose history starts mid-position — a SELL
    exceeding the BUYs we can see, e.g. a broker activity feed that doesn't reach back
    to the opening BUY (live case 2026-06-12: E*TRADE via SnapTrade, ``SELL 27 SQ``
    with no prior BUY in the feed). Here that group ALONE is skipped — WARN-logged and
    returned in the flag list (the recording surfaces for this degradation) — instead
    of one ticker's history gap 500ing every portfolio view that builds a ledger.

    Cash-only transactions (deposits/dividends/interest/fees, no ticker) group per
    account under ticker ``""`` and can never raise.
    """
    groups: dict[tuple[uuid.UUID | None, str], list[Transaction]] = {}
    for account_id, txn in txns:
        groups.setdefault((account_id, txn.ticker), []).append(txn)

    merged = Ledger()
    incomplete: list[IncompleteHistory] = []
    for (account_id, ticker), group in groups.items():
        try:
            ledger = build_ledger(group)
        except ValueError as e:
            incomplete.append(IncompleteHistory(account_id=account_id, ticker=ticker, error=str(e)))
            if log:
                logger.warning(
                    "Incomplete history for %s (account %s) — excluded from derived analytics: %s",
                    ticker, account_id, e,
                )
            continue
        for t, lots in ledger.open_lots.items():
            merged.open_lots.setdefault(t, []).extend(lots)
        merged.realized.extend(ledger.realized)
        merged.cash += ledger.cash
    for lots in merged.open_lots.values():
        lots.sort(key=lambda lot: lot.open_date)
    merged.realized.sort(key=lambda r: r.close_date)
    return merged, incomplete


def load_ledger(
    session: Session,
    tenant_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    account_id: uuid.UUID | None = None,
    account_ids: Collection[uuid.UUID] | None = None,
) -> tuple[Ledger, list[IncompleteHistory]]:
    """Build the FIFO ledger for a portfolio (or a single/subset of accounts) from its
    transactions. Returns ``(ledger, incomplete)`` — ``incomplete`` flags the
    per-(account, ticker) groups whose history could not be replayed (already
    WARN-logged); the ledger carries everything else."""
    return build_portfolio_ledger(
        engine_transactions_by_account(session, tenant_id, portfolio_id, account_id, account_ids)
    )


def _position_rows(
    session: Session,
    tenant_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    account_id: uuid.UUID | None = None,
    account_ids: Collection[uuid.UUID] | None = None,
):
    """Fetch ``(quantity, avg_cost, market_value_local, as_of, ticker)`` for
    broker-reported positions in a portfolio (snapshot-sourced accounts: Flex/SnapTrade),
    optionally one account or a SET of accounts. ``market_value_local`` is the broker's
    native value (the foreign-listing valuation fallback) and may be None. ``account_ids``
    narrows to a set (an empty set yields no rows)."""
    stmt = (
        select(
            models.Position.quantity,
            models.Position.avg_cost,
            models.Position.market_value_local,
            models.Position.as_of,
            models.Security.symbol,
        )
        .join(models.Account, models.Position.account_id == models.Account.id)
        .join(models.Security, models.Position.security_id == models.Security.id)
        .where(models.Position.tenant_id == tenant_id, models.Account.portfolio_id == portfolio_id)
    )
    if account_id is not None:
        stmt = stmt.where(models.Position.account_id == account_id)
    if account_ids is not None:
        stmt = stmt.where(models.Position.account_id.in_(account_ids))
    return session.execute(stmt).all()


def _currency_by_symbol(session: Session, symbols: list[str]) -> dict[str, str]:
    """Authoritative native currency per held symbol, from the global Security master
    (first row per symbol — stable, mirrors the price service). Symbols with no
    Security row default to USD."""
    if not symbols:
        return {}
    rows = session.execute(
        select(models.Security.symbol, models.Security.currency)
        .where(models.Security.symbol.in_(symbols))
        .order_by(models.Security.symbol, models.Security.id)
    ).all()
    out: dict[str, str] = {}
    for symbol, currency in rows:
        out.setdefault(symbol, currency or "USD")
    return out


_CUSIP_LEN = 9
# US Treasury issues share the "912" CUSIP issuer prefix (bills 912796/912797, notes/bonds
# 9128xx + 91282x, long bonds 912810). A strong, no-guess treasury signal off the bare CUSIP.
_TREASURY_CUSIP_PREFIX = "912"


def _has_token(text: str, token: str) -> bool:
    """Whole-token match (so 'cd' matches in 'brokered cd 4.5%' but not inside 'cad')."""
    return f" {token} " in f" {text} "


def _bond_family_subtype(asset_class: str, ticker: str, name: str | None) -> str:
    """Within the fixed-income family, distinguish ``treasury`` / ``cd`` / generic ``bond``
    (metron-ops#114). No-guess: only a confident signal (asset_class keyword, the Treasury
    CUSIP prefix, or a whole-token name match) promotes off the generic ``bond`` — anything
    ambiguous stays ``bond`` and the user can reclassify via the Type override."""
    ac = asset_class  # already lowercased
    nm = (name or "").lower()
    t = (ticker or "").strip()
    if (
        "treasur" in ac
        or "treasur" in nm
        or _has_token(nm, "t-bill")
        or _has_token(nm, "t-note")
        or _has_token(nm, "t-bond")
    ):
        return "treasury"
    if t.isdigit() and len(t) == _CUSIP_LEN and t.startswith(_TREASURY_CUSIP_PREFIX):
        return "treasury"
    if "certificate of deposit" in ac or "certificate of deposit" in nm or _has_token(ac, "cd") or _has_token(nm, "cd"):
        return "cd"
    return "bond"


def classify_security_type(asset_class: str | None, ticker: str, name: str | None) -> str:
    """Asset class for grouping holdings: one of ``cash`` / ``treasury`` / ``cd`` / ``bond``
    / ``equity`` / ``etf`` / ``fund`` / ``option`` / ``other`` (metron-ops#47, fixed-income
    split metron-ops#114).

    The Security master's ``asset_class`` (connector-supplied: EQUITY / ETF / FUND /
    OPTION / CASH / OTHER, lowercased) is authoritative. When it's absent, infer: a
    9-digit numeric symbol is a CUSIP — typically a bond/CD/treasury that surfaces as an
    unreadable number; otherwise fall back to name keywords, else equity (a normal
    alpha ticker). The fixed-income family is split into treasury / cd / generic bond by
    ``_bond_family_subtype`` (no-guess — ambiguous stays ``bond``)."""
    ac = (asset_class or "").strip().lower()
    if ac:
        if "cash" in ac:
            return "cash"
        if (
            "bond" in ac
            or "fixed" in ac
            or "treasur" in ac
            or "certificate of deposit" in ac
            or _has_token(ac, "cd")
        ):
            return _bond_family_subtype(ac, ticker, name)
        if ac == "etf":
            return "etf"
        if ac == "fund" or "mutual" in ac:
            return "fund"
        if ac == "option":
            return "option"
        if ac in ("equity", "stock"):
            return "equity"
        return "other"
    t = (ticker or "").strip()
    if t.isdigit() and len(t) == _CUSIP_LEN:
        return _bond_family_subtype(ac, ticker, name)  # 9-digit CUSIP → fixed-income family
    nm = (name or "").lower()
    if "money market" in nm or "cash" in nm:
        return "cash"
    if "bond" in nm or "treasury" in nm or _has_token(nm, "cd"):
        return _bond_family_subtype(ac, ticker, name)
    return "equity"


def _security_meta_by_symbol(
    session: Session, symbols: list[str]
) -> dict[str, tuple[str | None, str | None]]:
    """``{symbol: (asset_class, name)}`` from the Security master (first row per symbol,
    mirroring ``_currency_by_symbol``). Drives the holding security-type classification."""
    if not symbols:
        return {}
    rows = session.execute(
        select(models.Security.symbol, models.Security.asset_class, models.Security.name)
        .where(models.Security.symbol.in_(symbols))
        .order_by(models.Security.symbol, models.Security.id)
    ).all()
    out: dict[str, tuple[str | None, str | None]] = {}
    for symbol, asset_class, name in rows:
        out.setdefault(symbol, (asset_class, name))
    return out


def _scoped_account_ids(
    session: Session,
    portfolio_id: uuid.UUID,
    account_id: uuid.UUID | None,
    account_ids: Collection[uuid.UUID] | None,
) -> set[uuid.UUID]:
    """The account ids in scope — the portfolio's accounts, narrowed by ``account_id``
    (one) or ``account_ids`` (a set, possibly empty)."""
    stmt = select(models.Account.id).where(models.Account.portfolio_id == portfolio_id)
    if account_id is not None:
        stmt = stmt.where(models.Account.id == account_id)
    if account_ids is not None:
        stmt = stmt.where(models.Account.id.in_(account_ids))
    return {row[0] for row in session.execute(stmt)}


def _snapshot_sourced_account_ids(
    session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID
) -> set[uuid.UUID]:
    """Accounts whose CURRENT holdings come from the broker ``positions`` snapshot
    (Flex/SnapTrade). Their transactions — which SnapTrade/Flex ALSO populate, for
    realized-gain/dividend history — must NOT also feed the current-holdings ledger,
    or shares + cost basis double-count.

    Classified by **broker source**, not by having position rows: an account that sold
    everything has activities and ZERO position rows, and its empty snapshot is still
    authoritative (live case 2026-06-12: two emptied E*TRADE accounts leaked their
    partial activity history into the holdings ledger). The position-rows check is kept
    as a defensive union for any legacy snapshot data whose broker string predates
    ``SNAPSHOT_SOURCES``."""
    by_broker = (
        select(models.Account.id)
        .where(
            models.Account.tenant_id == tenant_id,
            models.Account.portfolio_id == portfolio_id,
            models.Account.broker.in_(SNAPSHOT_SOURCES),
        )
    )
    by_positions = (
        select(models.Position.account_id)
        .join(models.Account, models.Position.account_id == models.Account.id)
        .where(models.Position.tenant_id == tenant_id, models.Account.portfolio_id == portfolio_id)
        .distinct()
    )
    return {row[0] for row in session.execute(by_broker)} | {
        row[0] for row in session.execute(by_positions)
    }


def holdings(
    session: Session,
    tenant_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    account_id: uuid.UUID | None = None,
    account_ids: Collection[uuid.UUID] | None = None,
) -> list[Holding]:
    """Current open positions with share-weighted average cost + total (native) cost basis.

    Unions the two ingestion models, aggregated by ticker: positions **derived from
    the transaction ledger** (CSV/OFX accounts) and positions **reported directly by
    the broker** (Flex/SnapTrade → the ``positions`` table). The two are NOT mutually
    exclusive on disk — SnapTrade/Flex populate BOTH ``transactions`` (activities, for
    realized-gain/dividend history) AND ``positions`` for the same account — so a naive
    union double-counts shares + cost basis (a phantom loss when the broker market value
    isn't doubled too, e.g. funds with no cached price bar). To keep one account = one
    current-holdings source, an account that has ANY position snapshot is treated as
    snapshot-sourced and its transactions are EXCLUDED from the ledger side here; only
    ledger-sourced accounts (CSV/OFX — no positions) contribute via the ledger. A ticker
    held in both a CSV account and a Flex account still correctly sums across accounts.
    With ``account_id`` (one) or ``account_ids`` (a set) the union is scoped — the SAME
    filter is applied to BOTH sources so the two never desync.

    All monetary values here are in the instrument's NATIVE currency — FX conversion to
    the portfolio base happens in ``valued_holdings``."""
    # ticker → [total_shares, total_cost_basis, broker_market_value_local | None]
    agg: dict[str, list[float]] = {}
    broker_mv: dict[str, float] = {}
    broker_as_of: dict[str, date] = {}

    # Ledger side: only accounts in scope that have NO broker position snapshot. This is
    # what prevents the SnapTrade/Flex "activities + positions" double-count.
    ledger_ids = _scoped_account_ids(session, portfolio_id, account_id, account_ids) - (
        _snapshot_sourced_account_ids(session, tenant_id, portfolio_id)
    )
    ledger, _incomplete = load_ledger(session, tenant_id, portfolio_id, account_ids=ledger_ids)
    for ticker in ledger.open_lots:
        shares, avg_cost = ledger.position(ticker)
        if shares > 0:
            agg.setdefault(ticker, [0.0, 0.0])
            agg[ticker][0] += shares
            agg[ticker][1] += shares * avg_cost

    for quantity, avg_cost, mv_local, as_of, ticker in _position_rows(
        session, tenant_id, portfolio_id, account_id, account_ids
    ):
        qty = float(quantity)
        if qty <= 0:
            continue
        agg.setdefault(ticker, [0.0, 0.0])
        agg[ticker][0] += qty
        agg[ticker][1] += qty * float(avg_cost)
        if mv_local is not None:
            broker_mv[ticker] = broker_mv.get(ticker, 0.0) + float(mv_local)
            if as_of is not None and (ticker not in broker_as_of or as_of > broker_as_of[ticker]):
                broker_as_of[ticker] = as_of

    ccy = _currency_by_symbol(session, list(agg))
    out: list[Holding] = []
    for t, (shares, basis) in sorted(agg.items()):
        # Per-share broker price from the summed native market value (qty-weighted).
        bm = broker_mv.get(t)
        out.append(
            Holding(
                ticker=t,
                quantity=shares,
                avg_cost=basis / shares if shares else 0.0,
                cost_basis=basis,
                currency=ccy.get(t, "USD"),
                broker_market_price=(bm / shares) if (bm is not None and shares) else None,
                broker_as_of=broker_as_of.get(t),
            )
        )
    return out


def _apply_valuation(h: Holding, prices: dict, fx_rates: dict[str, float | None]) -> None:
    """Fold a cached price + FX rate into one native ``Holding``, in place.

    The single valuation rule shared by ``valued_holdings`` and
    ``valued_holdings_by_account`` so per-portfolio and per-account views value
    identically. Native price = cached close, else broker-native fallback; base fields
    stay None when no FX rate is cached (never fabricates 1 unit foreign = 1 USD)."""
    h.fx_rate = fx_rates.get(h.currency)
    # Cost basis → base (needs only the FX rate, not a price).
    if h.fx_rate is not None:
        h.cost_basis_base = h.cost_basis * h.fx_rate
    # Native price: cached close first, broker-native fallback.
    point = prices.get(h.ticker)
    if point is not None:
        h.last_price = point.close
        h.last_price_date = point.bar_date
        h.last_price_from_close = True
    elif h.broker_market_price is not None:
        h.last_price = h.broker_market_price
        h.last_price_date = h.broker_as_of
    if h.last_price is None:
        return
    h.market_value_local = h.last_price * h.quantity
    # Currency-invariant return ratio (native over native).
    h.unrealized_pct = ((h.market_value_local - h.cost_basis) / h.cost_basis) if h.cost_basis else None
    if h.fx_rate is not None:
        h.market_value = h.market_value_local * h.fx_rate
        if h.cost_basis_base is not None:
            h.unrealized_gain = h.market_value - h.cost_basis_base


def valued_holdings(
    session: Session,
    tenant_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    account_id: uuid.UUID | None = None,
    account_ids: Collection[uuid.UUID] | None = None,
    *,
    prices: dict[str, ClosePoint] | None = None,
) -> list[Holding]:
    """Holdings enriched with market value, converted to the portfolio base currency.

    Each holding's NATIVE price comes from the latest cached close, falling back to the
    broker-reported native price (so a foreign listing yfinance can't resolve still
    values). The native price/value populate the ``_local`` fields; the FX rate
    (``base`` per 1 unit of the holding's currency) folds them into the base-currency
    ``market_value`` / ``cost_basis_base`` / ``unrealized_gain``.

    ``prices`` overrides the price source (``{ticker: ClosePoint}``): the LIVE intraday
    valuation passes intraday last-prices merged over EOD closes so NAV recomputes from
    fresh balances (metron-ops#79). Default ``None`` reads the latest cached EOD close —
    the persisted NAV-history snapshot path always uses this, never intraday.

    Never fabricates: an unpriced holding keeps its valuation None (shown at cost), and a
    foreign holding with **no cached FX rate** keeps its BASE fields None (the native
    ``_local`` values are still shown) rather than mis-counting 1 unit foreign as 1 USD.
    Composes with account_id / account_ids, so per-account views value cleanly too."""
    held = holdings(session, tenant_id, portfolio_id, account_id, account_ids)
    if not held:
        return held
    base = _base_currency(session, portfolio_id)
    if prices is None:
        prices = price_service.latest_close_by_symbol(session, [h.ticker for h in held])
    fx_rates = fx_service.rates_to_base(session, [h.currency for h in held], base=base)
    meta = _security_meta_by_symbol(session, [h.ticker for h in held])
    user_labels = labels.labels_by_symbol(session, tenant_id, [h.ticker for h in held])
    for h in held:
        _apply_valuation(h, prices, fx_rates)
        asset_class, name = meta.get(h.ticker, (None, None))
        h.security_type = classify_security_type(asset_class, h.ticker, name)
        h.user_label = user_labels.get(h.ticker)
    return held


def valued_holdings_by_account(
    session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID
) -> dict[uuid.UUID, list[Holding]]:
    """Per-account valued holdings for EVERY account in the portfolio, in one shot.

    Powers the Accounts panel's per-account cost basis / market value / unrealized. Each
    account's holdings come from the existing per-account ``holdings`` union (ledger XOR
    broker-snapshot, FIFO replayed per account so cost basis is correct), but the price +
    FX lookups run **once** over the union of all tickers/currencies — no N round-trips.
    Returns ``{account_id: [Holding, …]}`` (empty list for an account with no open
    positions)."""
    acct_ids = list(
        session.scalars(
            select(models.Account.id).where(
                models.Account.tenant_id == tenant_id, models.Account.portfolio_id == portfolio_id
            )
        ).all()
    )
    per_account = {
        aid: holdings(session, tenant_id, portfolio_id, account_id=aid) for aid in acct_ids
    }
    all_held = [h for hs in per_account.values() for h in hs]
    if not all_held:
        return per_account
    base = _base_currency(session, portfolio_id)
    prices = price_service.latest_close_by_symbol(session, [h.ticker for h in all_held])
    fx_rates = fx_service.rates_to_base(session, [h.currency for h in all_held], base=base)
    # Classify asset class per holding (one meta lookup over the union) so per-account
    # callers can detect FUND legs — the late-fund-NAV provisional/reconcile path needs
    # `security_type` at account grain, matching what `valued_holdings` already stamps.
    meta = _security_meta_by_symbol(session, [h.ticker for h in all_held])
    for hs in per_account.values():
        for h in hs:
            _apply_valuation(h, prices, fx_rates)
            asset_class, name = meta.get(h.ticker, (None, None))
            h.security_type = classify_security_type(asset_class, h.ticker, name)
    return per_account


def valued_holdings_by_account_flat(
    session: Session,
    tenant_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    account_ids: Collection[uuid.UUID] | None = None,
    *,
    prices: dict[str, ClosePoint] | None = None,
) -> list[Holding]:
    """Per-account valued holdings as a FLAT list — one row per (account, ticker), each
    tagged with ``account_id`` + ``account_label`` (metron-ops#114).

    The "uncombined" Holdings view: a security held in N accounts shows as N rows. Scoped
    to ``account_ids`` (None = every account in the portfolio). Prices + FX resolve ONCE
    over the union of tickers/currencies (no N round-trips), exactly like
    ``valued_holdings_by_account``; ``prices`` overrides the source (the intraday overlay).
    Native + base fields populate identically to the consolidated ``valued_holdings`` path,
    so the two views value the same security identically."""
    acct_rows = session.execute(
        select(
            models.Account.id, models.Account.nickname, models.Account.name, models.Account.external_id
        ).where(
            models.Account.tenant_id == tenant_id,
            models.Account.portfolio_id == portfolio_id,
        )
    ).all()
    scope = set(account_ids) if account_ids is not None else None
    label_of: dict[uuid.UUID, str] = {}
    aids: list[uuid.UUID] = []
    for aid, nickname, name, external_id in acct_rows:
        if scope is not None and aid not in scope:
            continue
        aids.append(aid)
        label_of[aid] = nickname or name or external_id or str(aid)

    flat: list[Holding] = []
    for aid in aids:
        for h in holdings(session, tenant_id, portfolio_id, account_id=aid):
            h.account_id = aid
            h.account_label = label_of[aid]
            flat.append(h)
    if not flat:
        return flat
    base = _base_currency(session, portfolio_id)
    if prices is None:
        prices = price_service.latest_close_by_symbol(session, [h.ticker for h in flat])
    fx_rates = fx_service.rates_to_base(session, [h.currency for h in flat], base=base)
    meta = _security_meta_by_symbol(session, [h.ticker for h in flat])
    user_labels = labels.labels_by_symbol(session, tenant_id, [h.ticker for h in flat])
    for h in flat:
        _apply_valuation(h, prices, fx_rates)
        asset_class, name = meta.get(h.ticker, (None, None))
        h.security_type = classify_security_type(asset_class, h.ticker, name)
        h.user_label = user_labels.get(h.ticker)
    flat.sort(key=lambda h: (h.ticker, h.account_label or ""))
    return flat


def _base_currency(session: Session, portfolio_id: uuid.UUID) -> str:
    """The portfolio's base/reporting currency (defaults to USD)."""
    portfolio = session.get(models.Portfolio, portfolio_id)
    return (portfolio.base_currency if portfolio else "USD") or "USD"


def _stored_realized_lots(
    session: Session, tenant_id: uuid.UUID, account_ids: Collection[uuid.UUID]
) -> list[tuple[uuid.UUID, str, RealizedGain]]:
    """Broker-reported authoritative closed lots (e.g. IBKR ``fifoPnlRealized``) for the
    given accounts → ``(account_id, currency, RealizedGain)``. These exist for brokers
    with no replayable trade feed; their gains are correct regardless of import window
    (the broker FIFO-matched against full history) — metron-ops#81."""
    if not account_ids:
        return []
    rows = session.scalars(
        select(models.RealizedLot).where(
            models.RealizedLot.tenant_id == tenant_id,
            models.RealizedLot.account_id.in_(list(account_ids)),
        )
    ).all()
    return [
        (
            row.account_id,
            row.currency or "USD",
            RealizedGain(
                ticker=row.ticker,
                open_date=row.open_date,
                close_date=row.close_date,
                quantity=float(row.quantity),
                proceeds=float(row.proceeds),
                cost_basis=float(row.cost_basis),
            ),
        )
        for row in rows
    ]


def realized(
    session: Session,
    tenant_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    account_id: uuid.UUID | None = None,
    account_ids: Collection[uuid.UUID] | None = None,
) -> list[RealizedLot]:
    """Closed lots with proceeds, basis, gain, and holding-period classification.

    Two disjoint sources, merged: the FIFO ledger replayed from transactions (activity-feed
    brokers), and broker-reported authoritative closed lots (e.g. IBKR ``fifoPnlRealized``,
    which carry the correct gain regardless of import window — metron-ops#81). An account
    with stored lots is excluded from the replay so a disposal is never counted twice.

    Native amounts are converted to the portfolio base currency at the FX rate **as of
    the close date** (the rate when the gain was realized). A lot whose currency has no
    cached as-of rate keeps its base fields None (native still shown). Scopes to one
    account (``account_id``) or a set (``account_ids``)."""
    base = _base_currency(session, portfolio_id)
    scope = _scoped_account_ids(session, portfolio_id, account_id, account_ids)
    stored = _stored_realized_lots(session, tenant_id, scope)
    authoritative = {aid for aid, _ccy, _rg in stored}
    replay_ids = scope - authoritative

    # (currency_override | None, RealizedGain): stored lots carry their own currency; a
    # replayed lot resolves currency from the security.
    merged: list[tuple[str | None, RealizedGain]] = [(ccy, rg) for _aid, ccy, rg in stored]
    if replay_ids:
        ledger, _incomplete = load_ledger(session, tenant_id, portfolio_id, account_ids=replay_ids)
        merged += [(None, r) for r in ledger.realized]

    ccy_by_ticker = _currency_by_symbol(session, [rg.ticker for ccy, rg in merged if ccy is None])
    out: list[RealizedLot] = []
    for ccy, r in sorted(merged, key=lambda x: x[1].close_date):
        currency = ccy or ccy_by_ticker.get(r.ticker, base)
        rate = fx_service.rate_as_of(session, currency, r.close_date, base=base)
        out.append(
            RealizedLot(
                ticker=r.ticker,
                open_date=r.open_date,
                close_date=r.close_date,
                quantity=r.quantity,
                proceeds=r.proceeds,
                cost_basis=r.cost_basis,
                gain=r.gain,
                long_term=r.long_term,
                currency=currency,
                fx_rate=rate,
                gain_base=(r.gain * rate) if rate is not None else None,
                proceeds_base=(r.proceeds * rate) if rate is not None else None,
                cost_basis_base=(r.cost_basis * rate) if rate is not None else None,
            )
        )
    return out


def transactions(
    session: Session,
    tenant_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    account_id: uuid.UUID | None = None,
    account_ids: Collection[uuid.UUID] | None = None,
) -> list[TransactionRow]:
    """The portfolio's stored transactions, oldest first (optionally one account or a set)."""
    return [
        TransactionRow(
            trade_date=row.trade_date,
            txn_type=row.txn_type,
            ticker=ticker or "",
            quantity=float(row.quantity),
            price=float(row.price),
            amount=float(row.amount),
            fees=float(row.fees),
            currency=row.currency,
        )
        for row, ticker in _portfolio_rows(session, tenant_id, portfolio_id, account_id, account_ids)
    ]


@dataclass
class AccountInfo:
    account_id: uuid.UUID
    broker: str
    external_id: str
    name: str
    currency: str
    nickname: str | None = None
    institution: str | None = None
    account_type: str | None = None
    tax_treatment: str | None = None
    taxable: bool = True
    # Per-account valuation (base currency), from valued_holdings_by_account. cost_basis
    # is price-free (always known when convertible); market_value / unrealized_gain are
    # None until priced. n_unconverted flags holdings excluded for want of a cached FX rate.
    cost_basis_base: float | None = None
    market_value: float | None = None
    unrealized_gain: float | None = None
    n_unconverted: int = 0
    # Per-account period returns (metron-ops#87) — enriched by the accounts endpoint via
    # performance.account_period_returns. Day legs need the intraday feed; YTD/LTM derive
    # from the per-account reconstructed NAV series. None until enriched / when unavailable.
    overnight_pct: float | None = None
    intraday_pct: float | None = None
    day_pct: float | None = None
    ytd_pct: float | None = None
    ltm_pct: float | None = None


@dataclass
class PortfolioSummary:
    base_currency: str
    n_accounts: int
    n_holdings: int
    total_cost_basis: float
    realized_st: float
    realized_lt: float
    dividends: float
    interest: float
    distributions: float = 0.0  # taxable withdrawals from tax-deferred accounts (ordinary income)
    # YTD realized gains for the Overview cards (base currency). Taxable (the tax-relevant
    # figure) keeps the ST/LT split; tax-advantaged is a single never-taxed total.
    realized_st_ytd: float = 0.0
    realized_lt_ytd: float = 0.0
    realized_ytd_taxadv: float = 0.0
    # Valuation — None when no holding has a cached price (price-free path); otherwise
    # the sum over priced holdings (an unpriced holding contributes only its cost basis).
    # All monetary fields are in ``base_currency``.
    market_value: float | None = None
    unrealized_gain: float | None = None
    # Holdings excluded from the base-currency totals because their FX rate isn't cached
    # yet (foreign listing, no ``{CCY}USD=X`` bar) — surfaced so the UI can flag a
    # partial total rather than silently undercount.
    n_unconverted: int = 0

    @property
    def realized_total(self) -> float:
        return self.realized_st + self.realized_lt

    @property
    def taxable_income(self) -> float:
        return self.realized_total + self.dividends + self.interest + self.distributions


def income(
    session: Session,
    tenant_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    *,
    account_ids: Collection[uuid.UUID] | None = None,
    distribution_account_ids: Collection[uuid.UUID] | None = None,
) -> list[YearlyIncome]:
    """Per-year realized taxable income — realized ST/LT gains + dividends + interest +
    tax-deferred distributions.

    Realized gains come from the FIFO ledger; dividends/interest are summed directly
    from the cash transactions (they never enter the lot ledger). Every native amount is
    converted to the portfolio base currency at the FX rate **as of its event date**
    (close date for a realized lot, trade date for a dividend/interest) — so a HKD
    dividend lands in the USD income total at the rate it was actually worth, not today's.
    A row whose currency has no cached as-of rate is excluded (never summed at face
    value). Newest year first. ``account_ids`` restricts to a set of accounts (e.g. the
    taxable subset, so an IRA's dividends don't inflate a taxable-income figure).

    ``distribution_account_ids`` (the tax-deferred accounts, computed by the caller from
    the *candidate* scope, NOT the taxable subset) adds their WITHDRAWAL transactions as
    a separate **distributions** column — taxable ordinary income for a retiree even
    though the account is otherwise excluded from the taxable view (metron-ops#62)."""
    base = _base_currency(session, portfolio_id)
    rows = _portfolio_rows(session, tenant_id, portfolio_id, account_ids=account_ids)
    # Broker-authoritative closed lots (IBKR) for accounts in scope — their realized comes
    # from the stored lots, NOT a FIFO replay, so exclude their rows from the realized
    # ledger to avoid double-counting (metron-ops#81). Their cash rows still feed
    # dividends/interest below (the loop runs over the full `rows`).
    stored = _stored_realized_lots(
        session, tenant_id, _scoped_account_ids(session, portfolio_id, None, account_ids)
    )
    authoritative = {aid for aid, _ccy, _rg in stored}
    ledger, _incomplete = build_portfolio_ledger(
        [(row.account_id, _to_engine_txn(row, ticker)) for row, ticker in rows if row.account_id not in authoritative]
    )

    # Realized gains → base at the close-date rate (rebuild each lot with base proceeds /
    # cost so its derived gain is in base; drop a lot we can't convert). Replayed lots
    # first, then the stored authoritative lots (their stored currency).
    ccy_by_ticker = _currency_by_symbol(session, [r.ticker for r in ledger.realized])
    realized_base: list[RealizedGain] = []
    replayed = [(ccy_by_ticker.get(r.ticker, base), r) for r in ledger.realized]
    for ccy, r in replayed + [(c, rg) for _aid, c, rg in stored]:
        rate = fx_service.rate_as_of(session, ccy, r.close_date, base=base)
        if rate is None:
            continue
        realized_base.append(
            RealizedGain(
                ticker=r.ticker,
                open_date=r.open_date,
                close_date=r.close_date,
                quantity=r.quantity,
                proceeds=r.proceeds * rate,
                cost_basis=r.cost_basis * rate,
            )
        )

    dividends: dict[int, float] = defaultdict(float)
    interest: dict[int, float] = defaultdict(float)
    for row, _ticker in rows:
        if row.txn_type not in (TxnType.DIVIDEND.value, TxnType.INTEREST.value):
            continue
        rate = fx_service.rate_as_of(session, row.currency, row.trade_date, base=base)
        if rate is None:
            continue
        amount = float(row.amount) * rate
        bucket = dividends if row.txn_type == TxnType.DIVIDEND.value else interest
        bucket[row.trade_date.year] += amount

    # Tax-deferred distributions: WITHDRAWAL transactions from the caller-supplied
    # tax-deferred accounts are taxable ordinary income (Trad IRA / 401(k) withdrawals +
    # RMDs). Scanned from their own account scope — they're deliberately outside the
    # taxable ``account_ids`` above, so this is a separate query.
    distributions: dict[int, float] = defaultdict(float)
    if distribution_account_ids:
        for row, _ticker in _portfolio_rows(
            session, tenant_id, portfolio_id, account_ids=list(distribution_account_ids)
        ):
            if row.txn_type != TxnType.WITHDRAWAL.value:
                continue
            rate = fx_service.rate_as_of(session, row.currency, row.trade_date, base=base)
            if rate is None:
                continue
            distributions[row.trade_date.year] += abs(float(row.amount)) * rate

    return summarize_income_by_year(realized_base, dict(dividends), dict(interest), dict(distributions))


def accounts(session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID) -> list[AccountInfo]:
    """The portfolio's connected accounts (one row per broker account), with tags +
    derived taxable status + per-account valuation (cost basis / market value /
    unrealized, base currency). Always lists ALL accounts (the panel is the selector)."""
    from api.services import account_meta  # local import avoids a cycle (account_meta → models)

    rows = session.scalars(
        select(models.Account)
        .where(models.Account.tenant_id == tenant_id, models.Account.portfolio_id == portfolio_id)
        .order_by(models.Account.broker, models.Account.external_id)
    ).all()
    by_account = valued_holdings_by_account(session, tenant_id, portfolio_id)
    out: list[AccountInfo] = []
    for a in rows:
        held = by_account.get(a.id, [])
        convertible = [h for h in held if h.cost_basis_base is not None]
        priced = [h for h in held if h.market_value is not None]
        out.append(
            AccountInfo(
                account_id=a.id,
                broker=a.broker,
                external_id=a.external_id,
                name=a.name or "",
                currency=a.currency,
                nickname=a.nickname,
                institution=a.institution,
                account_type=a.account_type,
                tax_treatment=a.tax_treatment,
                taxable=account_meta.is_taxable(a),
                # Sum only over convertible/priced holdings (a foreign holding with no
                # cached FX rate is excluded + counted, never summed at native face value).
                cost_basis_base=sum(h.cost_basis_base for h in convertible) if convertible else None,
                market_value=sum(h.market_value for h in priced) if priced else None,
                unrealized_gain=(
                    sum(h.unrealized_gain for h in priced if h.unrealized_gain is not None)
                    if priced
                    else None
                ),
                n_unconverted=len(held) - len(convertible),
            )
        )
    return out


def foreign_transaction_currencies(
    session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID, *, base: str = "USD"
) -> tuple[list[str], date | None]:
    """The distinct non-base currencies that appear in a portfolio's transactions, plus
    the earliest transaction date across them — the span over which FX history must be
    backfilled so realized gains / dividends convert at their as-of-date rate."""
    base = (base or "USD").strip().upper()
    rows = session.execute(
        select(models.Transaction.currency, func.min(models.Transaction.trade_date))
        .join(models.Account, models.Transaction.account_id == models.Account.id)
        .where(
            models.Transaction.tenant_id == tenant_id,
            models.Account.portfolio_id == portfolio_id,
            func.upper(models.Transaction.currency) != base,
        )
        .group_by(models.Transaction.currency)
    ).all()
    currencies = [c for c, _ in rows if c]
    earliest = min((d for _, d in rows if d is not None), default=None)
    return currencies, earliest


def summary(
    session: Session,
    tenant_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    account_ids: Collection[uuid.UUID] | None = None,
    *,
    prices: dict[str, ClosePoint] | None = None,
) -> PortfolioSummary:
    """Portfolio-level totals for the home view — cost basis, realized, income, plus
    market value / unrealized P&L when prices are cached (None otherwise, never
    fabricated). ``account_ids`` scopes every total to the selected accounts; None =
    whole portfolio. ``prices`` overrides the price source (the LIVE intraday valuation
    passes intraday last over EOD close so the headline NAV is fresh — metron-ops#79)."""
    from api.services import account_meta  # local import avoids a cycle (account_meta → models)

    portfolio = session.get(models.Portfolio, portfolio_id)
    held = valued_holdings(session, tenant_id, portfolio_id, account_ids=account_ids, prices=prices)
    # Realized lots split by tax treatment so the headline can surface YTD realized the
    # same way it splits unrealized — the taxable figure carries the tax consequence; an
    # IRA/401(k)/Roth realization is never taxed. Two disjoint scoped calls union back to
    # the full scope (realized() is per-account, so the partition loses nothing) and give
    # the taxable-vs-tax-advantaged split for free.
    scope_ids = _scoped_account_ids(session, portfolio_id, None, account_ids)
    taxable_ids = account_meta.taxable_account_ids(session, tenant_id, portfolio_id) & scope_ids
    adv_ids = scope_ids - taxable_ids
    taxable_lots = realized(session, tenant_id, portfolio_id, account_ids=taxable_ids)
    adv_lots = realized(session, tenant_id, portfolio_id, account_ids=adv_ids)
    closed = taxable_lots + adv_lots
    this_year = date.today().year  # calendar-year YTD, matching the Tax page's year tag
    # Tax-deferred accounts in scope → their withdrawals are taxable distributions.
    deferred = account_meta.tax_deferred_account_ids(session, tenant_id, portfolio_id)
    if account_ids is not None:
        deferred &= set(account_ids)
    yearly = income(
        session, tenant_id, portfolio_id, account_ids=account_ids, distribution_account_ids=deferred
    )
    count_stmt = (
        select(func.count())
        .select_from(models.Account)
        .where(models.Account.tenant_id == tenant_id, models.Account.portfolio_id == portfolio_id)
    )
    if account_ids is not None:
        count_stmt = count_stmt.where(models.Account.id.in_(account_ids))
    n_accounts = session.scalar(count_stmt)
    priced = [h for h in held if h.market_value is not None]
    market_value = sum(h.market_value for h in priced) if priced else None
    unrealized_gain = (
        sum(h.unrealized_gain for h in priced if h.unrealized_gain is not None) if priced else None
    )
    # Cost basis totals only over holdings we could express in the base currency; a
    # foreign holding with no cached FX rate is excluded (and counted) rather than
    # summed at face value into a USD total.
    convertible = [h for h in held if h.cost_basis_base is not None]
    n_unconverted = len(held) - len(convertible)
    return PortfolioSummary(
        base_currency=portfolio.base_currency if portfolio else "USD",
        n_accounts=int(n_accounts or 0),
        n_holdings=len(held),
        total_cost_basis=sum(h.cost_basis_base for h in convertible),
        # Base-currency realized gains (close-date FX); a lot with no cached rate is
        # excluded rather than mixed into the base total at native face value.
        realized_st=sum(r.gain_base for r in closed if not r.long_term and r.gain_base is not None),
        realized_lt=sum(r.gain_base for r in closed if r.long_term and r.gain_base is not None),
        # YTD realized, split by tax treatment for the Overview cards. Taxable carries the
        # ST/LT breakdown (the tax-relevant figure); tax-advantaged is a single no-tax total.
        realized_st_ytd=sum(
            r.gain_base for r in taxable_lots
            if not r.long_term and r.gain_base is not None and r.close_date.year == this_year
        ),
        realized_lt_ytd=sum(
            r.gain_base for r in taxable_lots
            if r.long_term and r.gain_base is not None and r.close_date.year == this_year
        ),
        realized_ytd_taxadv=sum(
            r.gain_base for r in adv_lots
            if r.gain_base is not None and r.close_date.year == this_year
        ),
        dividends=sum(i.dividends for i in yearly),
        interest=sum(i.interest for i in yearly),
        distributions=sum(i.distributions for i in yearly),
        market_value=market_value,
        unrealized_gain=unrealized_gain,
        n_unconverted=n_unconverted,
    )
