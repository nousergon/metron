"""Layer-1 custodian reconciliation (metron-ops#216) — nightly independent check of
Metron's persisted positions/cash against a FRESH broker read, recording any
divergence beyond tolerance as a first-class ``models.ReconciliationBreak`` and
alerting on new/reopened breaks.

Deliberately does NOT reuse ``broker_sync.sync_*_for_portfolio`` (which persists the
fresh snapshot, overwriting Metron's state). Comparing "before" to "after" a persist
can't distinguish a real trade since the last sync from an actual data-integrity bug —
both look like a before/after delta. Instead this fetches a snapshot the SAME way
(``broker_sync.fetch_*_snapshot_for_portfolio``, no persist) and diffs it against
whatever is in the DB *right now* — an independent verification pass that doesn't
trust the regular sync path's own success signal, so a silent parsing/mapping bug in
that path (or a missed cron fire) still gets caught here.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.config import settings
from api.db import models
from api.services import broker_sync
from api.services.alerting import send_telegram_alert
from portfolio_analytics.ingestion.base import ConnectorSnapshot

logger = logging.getLogger(__name__)

_QTY_TOLERANCE = 1e-6  # a share-count mismatch is never "rounding"; this only absorbs float noise
_BROKERS = ("ibkr_flex", "snaptrade")


def _break_key(account_id: uuid.UUID, break_type: str, security_id: uuid.UUID | None) -> str:
    return f"{account_id}:{break_type}:{security_id or 'account'}"


def _cost_basis_tolerance(broker_value: float) -> float:
    """Absolute-or-relative band: the larger of a flat $ floor and a bps-of-value
    band, so a small position isn't flagged for cents of rounding and a large one
    isn't flagged for a fraction-of-a-percent FX conversion difference."""
    return max(
        settings.reconciliation_cash_tolerance_usd,
        abs(broker_value) * settings.reconciliation_cost_basis_tolerance_bps / 10_000,
    )


@dataclass
class _Break:
    account_id: uuid.UUID
    break_type: str
    security_id: uuid.UUID | None
    metron_value: float | None
    broker_value: float | None
    tolerance: float


@dataclass
class ReconcileResult:
    portfolios_checked: int = 0
    accounts_covered: int = 0
    breaks_open: int = 0
    breaks_new: int = 0
    breaks_resolved: int = 0
    fetch_failures: list[str] = field(default_factory=list)


def _diff_snapshot(
    session: Session, portfolio: models.Portfolio, snapshot: ConnectorSnapshot
) -> tuple[list[_Break], set[uuid.UUID]]:
    """Diff one broker snapshot (already fetched, not persisted) against the current
    DB state for the accounts it covers. Returns (breaks, covered_account_ids) —
    ``covered_account_ids`` is every DB account this snapshot could actually compare
    (used by the caller to scope stale-break resolution to accounts really re-checked,
    never to accounts skipped because the broker fetch failed)."""
    broker = snapshot.source
    account_numbers = {h.account_number for h in snapshot.holdings} | {a.number for a in snapshot.accounts}
    if not account_numbers:
        return [], set()
    db_accounts = {
        row.external_id: row
        for row in session.scalars(
            select(models.Account).where(
                models.Account.tenant_id == portfolio.tenant_id,
                models.Account.broker == broker,
                models.Account.external_id.in_(account_numbers),
            )
        ).all()
    }
    covered_account_ids = {row.id for row in db_accounts.values()}

    securities_by_id = {s.security_id: s for s in snapshot.securities}
    tickers = {s.ticker for s in snapshot.securities}
    db_securities = (
        {
            (row.symbol, row.currency): row
            for row in session.scalars(select(models.Security).where(models.Security.symbol.in_(tickers))).all()
        }
        if tickers
        else {}
    )

    db_positions = (
        {
            (row.account_id, row.security_id): row
            for row in session.scalars(
                select(models.Position).where(models.Position.account_id.in_(covered_account_ids))
            ).all()
        }
        if covered_account_ids
        else {}
    )

    breaks: list[_Break] = []
    matched_position_keys: set[tuple[uuid.UUID, uuid.UUID]] = set()

    for h in snapshot.holdings:
        db_account = db_accounts.get(h.account_number)
        sec = securities_by_id.get(h.security_id)
        if db_account is None or sec is None:
            continue  # brand-new account/security this run — the next real sync creates it; nothing to compare yet
        db_security = db_securities.get((sec.ticker, sec.currency))
        if db_security is None:
            continue
        key = (db_account.id, db_security.id)
        pos = db_positions.get(key)
        broker_cost_basis = h.cost_basis if h.cost_basis else h.avg_cost * h.quantity
        if pos is None:
            breaks.append(_Break(db_account.id, "missing_in_metron", db_security.id, None, h.quantity, 0.0))
            continue
        matched_position_keys.add(key)
        metron_qty = float(pos.quantity)
        if abs(metron_qty - h.quantity) > _QTY_TOLERANCE:
            breaks.append(
                _Break(db_account.id, "quantity", db_security.id, metron_qty, h.quantity, _QTY_TOLERANCE)
            )
        metron_cost_basis = float(pos.avg_cost) * metron_qty
        tol = _cost_basis_tolerance(broker_cost_basis)
        if abs(metron_cost_basis - broker_cost_basis) > tol:
            breaks.append(
                _Break(db_account.id, "cost_basis", db_security.id, metron_cost_basis, broker_cost_basis, tol)
            )

    # Positions Metron has for these accounts that the fresh broker read no longer reports at all
    # (a FULL_REFRESH snapshot is authoritative for CURRENT holdings — see base.SNAPSHOT_SOURCES).
    for (account_id, security_id), pos in db_positions.items():
        if (account_id, security_id) not in matched_position_keys:
            breaks.append(_Break(account_id, "missing_at_broker", security_id, float(pos.quantity), None, 0.0))

    for acct in snapshot.accounts:
        db_account = db_accounts.get(acct.number)
        if db_account is None:
            continue
        metron_cash = float(db_account.cash_balance_usd) if db_account.cash_balance_usd is not None else 0.0
        tol = settings.reconciliation_cash_tolerance_usd
        if abs(metron_cash - acct.cash_usd) > tol:
            breaks.append(_Break(db_account.id, "cash", None, metron_cash, acct.cash_usd, tol))

    return breaks, covered_account_ids


def _persist_breaks(
    session: Session, tenant_id: uuid.UUID, breaks: list[_Break], today: date
) -> tuple[list[models.ReconciliationBreak], int]:
    """Upsert each break on its ``break_key``. Returns (all rows touched, count newly
    opened or reopened — the set that should alert)."""
    if not breaks:
        return [], 0
    keys = [_break_key(b.account_id, b.break_type, b.security_id) for b in breaks]
    existing = {
        row.break_key: row
        for row in session.scalars(
            select(models.ReconciliationBreak).where(models.ReconciliationBreak.break_key.in_(keys))
        ).all()
    }
    rows: list[models.ReconciliationBreak] = []
    new_or_reopened = 0
    for b, key in zip(breaks, keys, strict=True):
        row = existing.get(key)
        if row is None:
            row = models.ReconciliationBreak(
                tenant_id=tenant_id,
                account_id=b.account_id,
                security_id=b.security_id,
                break_type=b.break_type,
                break_key=key,
                break_date=today,
                metron_value=b.metron_value,
                broker_value=b.broker_value,
                tolerance=b.tolerance,
            )
            session.add(row)
            new_or_reopened += 1
        else:
            row.metron_value = b.metron_value
            row.broker_value = b.broker_value
            row.tolerance = b.tolerance
            if row.resolved_at is not None:
                row.resolved_at = None
                row.alerted_at = None
                row.break_date = today  # reopening is a fresh occurrence for alerting purposes
                new_or_reopened += 1
        rows.append(row)
    session.flush()
    return rows, new_or_reopened


def _resolve_stale_breaks(
    session: Session, tenant_id: uuid.UUID, covered_account_ids: set[uuid.UUID], still_open_keys: set[str]
) -> int:
    """Mark resolved every unresolved break for an account we actually re-checked this
    run whose key did NOT reproduce. Scoped to ``covered_account_ids`` so an account we
    skipped (broker fetch failed) never has its breaks wrongly auto-resolved."""
    if not covered_account_ids:
        return 0
    candidates = session.scalars(
        select(models.ReconciliationBreak).where(
            models.ReconciliationBreak.tenant_id == tenant_id,
            models.ReconciliationBreak.account_id.in_(covered_account_ids),
            models.ReconciliationBreak.resolved_at.is_(None),
        )
    ).all()
    now = datetime.now(UTC)
    resolved = 0
    for row in candidates:
        if row.break_key not in still_open_keys:
            row.resolved_at = now
            resolved += 1
    return resolved


def _alert_text(portfolio: models.Portfolio, rows: list[models.ReconciliationBreak]) -> str:
    lines = [f"Metron reconciliation: {len(rows)} new/reopened break(s) — portfolio {portfolio.name}"]
    for row in rows[:20]:
        lines.append(
            f"  [{row.break_type}] account={row.account_id} security={row.security_id} "
            f"metron={row.metron_value} broker={row.broker_value} tol={row.tolerance}"
        )
    if len(rows) > 20:
        lines.append(f"  … and {len(rows) - 20} more (see reconciliation_breaks table)")
    return "\n".join(lines)


def reconcile_portfolio(session: Session, portfolio: models.Portfolio, *, today: date | None = None) -> ReconcileResult:
    """Run layer-1 reconciliation for one portfolio: fetch each connected broker fresh
    (no persist), diff against the current DB state, upsert breaks, resolve stale ones,
    alert on anything new. Best-effort per broker — a Flex outage never blocks the
    SnapTrade side, matching ``broker_sync``'s own degradation posture — but every
    fetch failure is alerted immediately (never silently skipped) and surfaces in
    ``ReconcileResult.fetch_failures`` so the CLI can fail loud."""
    today = today or datetime.now(UTC).date()
    result = ReconcileResult(portfolios_checked=1)
    all_breaks: list[_Break] = []
    covered_account_ids: set[uuid.UUID] = set()

    fetchers = {
        "ibkr_flex": broker_sync.fetch_flex_snapshot_for_portfolio,
        "snaptrade": broker_sync.fetch_snaptrade_snapshot_for_portfolio,
    }
    for broker in _BROKERS:
        try:
            snapshot = fetchers[broker](session, portfolio)
        except Exception as e:  # noqa: BLE001 — a fetch failure must alert, not crash the run
            msg = f"reconciliation fetch failed — portfolio={portfolio.id} broker={broker}: {e}"
            logger.error(msg)
            send_telegram_alert(f"⚠️ {msg}")
            result.fetch_failures.append(msg)
            continue
        if snapshot is None:
            continue
        breaks, covered = _diff_snapshot(session, portfolio, snapshot)
        all_breaks.extend(breaks)
        covered_account_ids |= covered

    rows, new_count = _persist_breaks(session, portfolio.tenant_id, all_breaks, today)
    still_open_keys = {row.break_key for row in rows}
    resolved_count = _resolve_stale_breaks(session, portfolio.tenant_id, covered_account_ids, still_open_keys)
    session.commit()

    result.accounts_covered = len(covered_account_ids)
    result.breaks_open = len(rows)
    result.breaks_new = new_count
    result.breaks_resolved = resolved_count

    if new_count:
        new_rows = [r for r in rows if r.alerted_at is None]
        if send_telegram_alert(_alert_text(portfolio, new_rows)):
            now = datetime.now(UTC)
            for row in new_rows:
                row.alerted_at = now
            session.commit()

    return result


def reconcile_all(session: Session) -> ReconcileResult:
    """Reconcile every non-reference portfolio — the nightly job's entrypoint."""
    from api.services.demo import REFERENCE_PORTFOLIO_ID

    total = ReconcileResult()
    portfolios = list(
        session.scalars(select(models.Portfolio).where(models.Portfolio.id != REFERENCE_PORTFOLIO_ID)).all()
    )
    for p in portfolios:
        r = reconcile_portfolio(session, p)
        total.portfolios_checked += r.portfolios_checked
        total.accounts_covered += r.accounts_covered
        total.breaks_open += r.breaks_open
        total.breaks_new += r.breaks_new
        total.breaks_resolved += r.breaks_resolved
        total.fetch_failures.extend(r.fetch_failures)
    return total
