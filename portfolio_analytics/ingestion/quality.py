"""Ingestion data-quality gates — layer 5 of the dashboard-accuracy verification
framework (metron-ops#219, part of EPIC metron-ops#210).

Three gates, all pure functions over the records crossing an ingestion boundary:

  * **Schema contract** (``check_snapshot_contract``) — every canonical record a
    connector hands to the bronze→silver boundary (accounts, securities, holdings,
    open lots, activities, realized lots) is checked against the invariants the
    downstream ledger and valuation code silently ASSUME: finite numbers, positive
    magnitudes, ISO-4217 currencies, known vocabularies, no future dates, and
    referential integrity between records in the same snapshot. Several of these
    are exactly the rows ``domain.ledger`` drops without a word (a BUY with
    ``quantity <= 0``, a SPLIT with a non-positive ratio) — the gate is what makes
    that drop visible.
  * **Price outlier / adjusted-close continuity** (``check_price_series``) — a
    close-to-close move beyond ``PRICE_OUTLIER_MAX_MOVE`` is flagged. A move that
    matches a RECORDED split's ratio is not an outlier — it is reported separately
    as a ``split_discontinuity``: the close series is unadjusted across that
    corporate action (the classic silent corrupter the epic names: a 2-for-1 split
    on an unadjusted series reads as a ~50% one-day "drop" that never happened).
  * **Stale price** — lives in ``api.services.data_quality`` because it must share
    the Holdings view's NYSE-calendar staleness predicate
    (``api.services.security_perf``); this module only owns the finding shape.

**FLAG MODE ONLY.** No thresholds were ratified for this layer (metron-ops#219 was
returned to the queue without them), so every gate here *returns findings* and
never drops, blocks, reorders or rewrites a record. The functions take their inputs
read-only and build new ``Finding`` objects; the caller logs them and carries on
with the unchanged data. Turning any gate into a blocking gate is a separate,
deliberate change once its false-positive rate has been observed in the logs.

Every threshold is a named module-level constant below, with the reasoning for its
value next to it, so tuning is a one-line reviewed change.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from portfolio_analytics.domain.ledger import TxnType
from portfolio_analytics.ingestion.base import ConnectorSnapshot
from portfolio_analytics.ingestion.schema import (
    ASSET_CASH,
    ASSET_EQUITY,
    ASSET_ETF,
    ASSET_FUND,
    ASSET_OPTION,
    ASSET_OTHER,
    TAX_DEFERRED,
    TAX_EXEMPT,
    TAX_TAXABLE,
)
from portfolio_analytics.prices.source import ClosePoint

# ── gate identifiers (the ``gate`` field of every Finding) ──────────────────────
GATE_CONTRACT = "schema_contract"
GATE_PRICE_OUTLIER = "price_outlier"
GATE_SPLIT_DISCONTINUITY = "split_discontinuity"
GATE_STALE_PRICE = "stale_price"
GATES = (GATE_CONTRACT, GATE_PRICE_OUTLIER, GATE_SPLIT_DISCONTINUITY, GATE_STALE_PRICE)

# ── thresholds ──────────────────────────────────────────────────────────────────
# Largest close-to-close simple return (either direction) accepted without a flag.
# 30% is chosen to sit BELOW the smallest common split's apparent move — a 3-for-2
# split on an unadjusted series reads as −33.3% — so every standard split ratio
# (3:2, 2:1, 3:1, 1:10 reverse…) trips it, while ordinary daily volatility of a
# listed equity/ETF, even on a bad earnings print, stays under it. Genuine >30% days
# do happen (biotech readouts, buyouts); in flag mode such a day costs one WARNING
# line, which is the right price for catching a mis-adjusted series.
PRICE_OUTLIER_MAX_MOVE = 0.30

# How close the observed price ratio must be to 1/split_ratio for a recorded split
# to "explain" the move. 10% absorbs a normal market day on top of the mechanical
# split jump (a 2-for-1 that also fell 4% on the day is still recognised) without
# letting an unrelated −30% move borrow a 2-for-1's explanation (0.70 × 2 = 1.40,
# 40% off).
SPLIT_RATIO_MATCH_TOLERANCE = 0.10

# Calendar-day slack either side of the (previous bar, this bar] window when
# matching a recorded split to a price move. Brokers report a split on the ex-date,
# the record date or the pay date depending on the feed, and those can differ by a
# couple of business days; 3 calendar days covers that without matching a split
# from a different week.
SPLIT_DATE_SLACK_DAYS = 3

# A record dated more than this many calendar days past "today" violates the
# contract. One day, not zero: a broker in an Asian time zone legitimately stamps
# tomorrow's date relative to a US-evening UTC clock.
MAX_FUTURE_DATE_DAYS = 1

# ISO-4217 shape (three upper-case letters). Deliberately a shape check, not a
# membership test against a currency list that would need maintaining.
_ISO_CCY = re.compile(r"^[A-Z]{3}$")

_ASSET_TYPES = frozenset({ASSET_EQUITY, ASSET_ETF, ASSET_FUND, ASSET_OPTION, ASSET_CASH, ASSET_OTHER})
_TAX_TREATMENTS = frozenset({"", TAX_TAXABLE, TAX_DEFERRED, TAX_EXEMPT})
_TRADE_TYPES = frozenset({TxnType.BUY, TxnType.SELL})
_CASH_TYPES = frozenset({TxnType.DIVIDEND, TxnType.INTEREST, TxnType.DEPOSIT, TxnType.WITHDRAWAL, TxnType.FEE})


@dataclass(frozen=True)
class Finding:
    """One data-quality observation. Never an instruction to change data.

    ``subject`` is a human locator (a symbol, or ``activity[3]`` for the fourth
    activity in a snapshot); ``observed``/``threshold`` carry the numbers that
    tripped the gate, recorded alongside so a later threshold change doesn't strand
    the context of an old log line (the same reason ``ReconciliationBreak`` records
    its tolerance)."""

    gate: str
    subject: str
    detail: str
    observed: float | None = None
    threshold: float | None = None
    as_of: date | None = None

    def render(self) -> str:
        """One-line, grep-stable rendering: ``[data-quality:<gate>] <subject>: <detail>``
        — the same shape as layer 2's ``[invariant:<label>]`` log lines."""
        return f"[data-quality:{self.gate}] {self.subject}: {self.detail}"


# ── helpers ─────────────────────────────────────────────────────────────────────
def _finite(x: object) -> bool:
    return isinstance(x, int | float) and not isinstance(x, bool) and math.isfinite(x)


def _as_date(d: date | datetime | None) -> date | None:
    if isinstance(d, datetime):
        return d.date()
    return d


class _Collector:
    """Accumulates contract findings for one snapshot with a shared ``today``."""

    def __init__(self, source: str, today: date) -> None:
        self.source = source
        self.latest_ok = today + timedelta(days=MAX_FUTURE_DATE_DAYS)
        self.findings: list[Finding] = []

    def flag(self, subject: str, detail: str, *, observed: float | None = None, as_of: date | None = None) -> None:
        self.findings.append(
            Finding(GATE_CONTRACT, f"{self.source}:{subject}", detail, observed=observed, as_of=as_of)
        )

    def finite(self, subject: str, name: str, value: object) -> bool:
        if _finite(value):
            return True
        self.flag(subject, f"{name}={value!r} is not a finite number")
        return False

    def currency(self, subject: str, value: object) -> None:
        if not isinstance(value, str) or not _ISO_CCY.match(value):
            self.flag(subject, f"currency={value!r} is not an ISO-4217 code")

    def not_future(self, subject: str, name: str, value: date | datetime | None) -> None:
        d = _as_date(value)
        if d is not None and d > self.latest_ok:
            self.flag(subject, f"{name}={d.isoformat()} is in the future", as_of=d)


# ── schema contract at the bronze → silver boundary ─────────────────────────────
def check_snapshot_contract(snapshot: ConnectorSnapshot, *, today: date | None = None) -> list[Finding]:
    """Check every record in a connector snapshot against the canonical contract.

    Read-only: the snapshot is not modified and nothing is filtered — the caller
    persists exactly what it would have persisted without this call. Returns one
    ``Finding`` per violation (empty when the snapshot is clean)."""
    c = _Collector(snapshot.source or "?", today or date.today())

    account_numbers: set[str] = set()
    for i, a in enumerate(snapshot.accounts):
        s = f"account[{i}]"
        if not a.number:
            c.flag(s, "account number is empty (it is the canonical join key)")
        account_numbers.add(a.number)
        c.finite(s, "nav_usd", a.nav_usd)
        c.finite(s, "cash_usd", a.cash_usd)
        c.currency(s, a.currency)
        if a.tax_treatment not in _TAX_TREATMENTS:
            c.flag(s, f"tax_treatment={a.tax_treatment!r} is outside the 3-way vocabulary")
        c.not_future(s, "as_of", a.as_of)

    security_ids: dict[str, tuple[str, str]] = {}
    for i, sec in enumerate(snapshot.securities):
        s = f"security[{i}]"
        if not sec.security_id:
            c.flag(s, "security_id is empty (holdings and activities join on it)")
        if not sec.ticker:
            c.flag(s, f"security {sec.security_id!r} has no ticker (the relational master keys on it)")
        c.currency(s, sec.currency)
        if sec.asset_type not in _ASSET_TYPES:
            c.flag(s, f"asset_type={sec.asset_type!r} is not a canonical ASSET_* value")
        identity = (sec.ticker, sec.currency)
        prior = security_ids.get(sec.security_id)
        if prior is not None and prior != identity:
            c.flag(s, f"security_id {sec.security_id!r} is declared twice with different ticker/currency "
                      f"({prior} vs {identity})")
        security_ids.setdefault(sec.security_id, identity)

    for i, h in enumerate(snapshot.holdings):
        s = f"holding[{i}]"
        if h.account_number not in account_numbers:
            c.flag(s, f"account {h.account_number!r} is not among this snapshot's accounts")
        if h.security_id not in security_ids:
            c.flag(s, f"security {h.security_id!r} is not in this snapshot's security master")
        for name in ("quantity", "cost_basis", "avg_cost", "market_value_local"):
            c.finite(s, name, getattr(h, name))
        if _finite(h.avg_cost) and h.avg_cost < 0:
            c.flag(s, f"avg_cost={h.avg_cost} is negative", observed=h.avg_cost)
        c.currency(s, h.currency)
        c.not_future(s, "as_of", h.as_of)

    for i, lot in enumerate(snapshot.open_lots):
        s = f"open_lot[{i}]"
        if lot.account_number not in account_numbers:
            c.flag(s, f"account {lot.account_number!r} is not among this snapshot's accounts")
        if c.finite(s, "quantity", lot.quantity) and lot.quantity == 0:
            c.flag(s, f"{lot.ticker} open lot has zero quantity", observed=0.0)
        c.finite(s, "cost_basis", lot.cost_basis)
        c.currency(s, lot.currency)
        c.not_future(s, "open_date", lot.open_date)

    for i, act in enumerate(snapshot.activities):
        _check_activity(c, f"activity[{i}]", act, account_numbers, security_ids)

    for i, (number, rg) in enumerate(snapshot.realized_lots):
        s = f"realized_lot[{i}]"
        if number not in account_numbers:
            c.flag(s, f"account {number!r} is not among this snapshot's accounts")
        for name in ("quantity", "proceeds", "cost_basis"):
            c.finite(s, name, getattr(rg, name))
        if _finite(rg.quantity) and rg.quantity <= 0:
            c.flag(s, f"{rg.ticker} closed lot quantity={rg.quantity} is not positive", observed=rg.quantity)
        if rg.open_date and rg.close_date and rg.close_date < rg.open_date:
            c.flag(s, f"{rg.ticker} closed {rg.close_date} before it opened {rg.open_date}", as_of=rg.close_date)
        c.not_future(s, "close_date", rg.close_date)

    return c.findings


def _check_activity(c: _Collector, s: str, act, account_numbers: set[str], security_ids: dict) -> None:
    if act.account_number not in account_numbers:
        c.flag(s, f"account {act.account_number!r} is not among this snapshot's accounts")
    if act.security_id and act.security_id not in security_ids:
        c.flag(s, f"security {act.security_id!r} is not in this snapshot's security master")
    for name in ("quantity", "price", "amount", "fees"):
        c.finite(s, name, getattr(act, name))
    c.currency(s, act.currency)
    c.not_future(s, "when", act.when)
    if _finite(act.fees) and act.fees < 0:
        c.flag(s, f"fees={act.fees} is negative (fees are a positive magnitude)", observed=act.fees)
    t = act.type
    if t in _TRADE_TYPES:
        if not act.security_id:
            c.flag(s, f"{t} has no security")
        if _finite(act.quantity) and act.quantity <= 0:
            # domain.ledger._buy/_sell silently ignore this row — make that visible.
            c.flag(s, f"{t} quantity={act.quantity} is not positive (the ledger skips it)", observed=act.quantity)
        if _finite(act.price) and act.price < 0:
            c.flag(s, f"{t} price={act.price} is negative", observed=act.price)
    elif t is TxnType.SPLIT:
        if not act.security_id:
            c.flag(s, "SPLIT has no security")
        if _finite(act.quantity) and act.quantity <= 0:
            c.flag(s, f"SPLIT ratio={act.quantity} is not positive (the ledger skips it)", observed=act.quantity)
    elif t in _CASH_TYPES and _finite(act.amount) and act.amount < 0:
        c.flag(s, f"{t} amount={act.amount} is negative (cash amounts are positive magnitudes; "
                  "the type carries direction)", observed=act.amount)


# ── price outlier / adjusted-close continuity ───────────────────────────────────
def check_price_point(subject: str, point: ClosePoint, *, today: date | None = None) -> list[Finding]:
    """Contract for one incoming close: finite, strictly positive, not future-dated."""
    out: list[Finding] = []
    if not _finite(point.close) or point.close <= 0:
        out.append(Finding(GATE_CONTRACT, subject, f"close={point.close!r} on {point.bar_date} is not a positive "
                                                   "finite price", as_of=point.bar_date))
    latest_ok = (today or date.today()) + timedelta(days=MAX_FUTURE_DATE_DAYS)
    if point.bar_date > latest_ok:
        out.append(Finding(GATE_CONTRACT, subject, f"bar_date={point.bar_date} is in the future",
                           as_of=point.bar_date))
    return out


def _split_ratio_for(splits: Iterable[tuple[date, float]], lo: date, hi: date) -> float | None:
    """The cumulative ratio of every recorded split dated in the slack-widened
    ``(lo, hi]`` window, or None when none falls there."""
    slack = timedelta(days=SPLIT_DATE_SLACK_DAYS)
    ratio = None
    for when, r in splits:
        if _finite(r) and r > 0 and lo - slack < when <= hi + slack:
            ratio = (ratio or 1.0) * r
    return ratio


def classify_move(
    subject: str,
    prev: ClosePoint,
    cur: ClosePoint,
    splits: Sequence[tuple[date, float]] = (),
) -> Finding | None:
    """Classify one close-to-close move. None when within ``PRICE_OUTLIER_MAX_MOVE``
    (or either close is unusable — that is the contract gate's finding, not this one).

    A move matching a recorded split's ratio (``cur/prev ≈ 1/ratio``) is a
    ``split_discontinuity`` — the series was not adjusted for that split — and
    anything else beyond the threshold is a ``price_outlier``."""
    if not (_finite(prev.close) and _finite(cur.close)) or prev.close <= 0 or cur.close <= 0:
        return None
    move = cur.close / prev.close - 1.0
    if abs(move) <= PRICE_OUTLIER_MAX_MOVE:
        return None
    ratio = _split_ratio_for(splits, prev.bar_date, cur.bar_date)
    if ratio is not None and abs((cur.close / prev.close) * ratio - 1.0) <= SPLIT_RATIO_MATCH_TOLERANCE:
        return Finding(
            GATE_SPLIT_DISCONTINUITY,
            subject,
            f"close moved {move:+.1%} from {prev.bar_date} to {cur.bar_date}, matching a recorded "
            f"{ratio:g}:1 split — the close series is not adjusted across this corporate action",
            observed=move,
            threshold=PRICE_OUTLIER_MAX_MOVE,
            as_of=cur.bar_date,
        )
    return Finding(
        GATE_PRICE_OUTLIER,
        subject,
        f"close moved {move:+.1%} from {prev.bar_date} ({prev.close:g}) to {cur.bar_date} ({cur.close:g}), "
        f"beyond ±{PRICE_OUTLIER_MAX_MOVE:.0%} with no recorded split to explain it",
        observed=move,
        threshold=PRICE_OUTLIER_MAX_MOVE,
        as_of=cur.bar_date,
    )


def check_price_series(
    subject: str,
    points: Iterable[ClosePoint],
    splits: Sequence[tuple[date, float]] = (),
    *,
    prior: ClosePoint | None = None,
    today: date | None = None,
) -> list[Finding]:
    """Contract-check every point and classify every consecutive move of a close
    series (sorted by date here; the input is not mutated). ``prior`` is the last
    already-cached close before the series, so the first incoming point is judged
    against history too. Duplicate dates compare only the last value per date."""
    by_date: dict[date, ClosePoint] = {}
    for p in points:
        by_date[p.bar_date] = p
    series = [by_date[d] for d in sorted(by_date)]
    out: list[Finding] = []
    for p in series:
        out.extend(check_price_point(subject, p, today=today))
    chain = ([prior] if prior is not None and (not series or prior.bar_date < series[0].bar_date) else []) + series
    for prev, cur in zip(chain, chain[1:], strict=False):
        f = classify_move(subject, prev, cur, splits)
        if f is not None:
            out.append(f)
    return out
