"""Operational maintenance jobs for a running Metron deploy.

``daily-refresh`` keeps every portfolio current: re-fetch EOD prices for held tickers,
record today's NAV snapshot, and pre-populate the derived analytics (Performance NAV
history, factor Risk, sector Attribution) so those pages flow data without the user
hunting for a "Compute" button. Run it once daily from a scheduler (systemd timer /
cron) so the personal setup stays a hands-off daily driver — no manual click needed.
Idempotent per day (re-running updates the same day's bars/snapshot/backfill).

The derived backfills are **best-effort**: each is wrapped so a yfinance hiccup logs a
WARN and the job moves on — it never costs the primary price refresh + NAV snapshot,
which have already committed.

    python -m api.maintenance daily-refresh

It operates directly on the database (no HTTP / auth) — it's an operator job, not a
tenant request — so it iterates every portfolio across the DB.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.config import settings
from api.db import models
from api.db.session import SessionLocal, create_all
from api.services import analytics, attribution, data_spine, fx, performance, risk
from api.services import calendar as calendar_svc
from api.services import prices as price_service

logger = logging.getLogger(__name__)


@dataclass
class RefreshResult:
    portfolios: int
    symbols: int
    prices_updated: int
    snapshots_recorded: int
    fx_rates_updated: int = 0
    snapshots_reconstructed: int = 0
    account_snapshots_recorded: int = 0  # per-account NAV snapshots written this run
    risk_computed: int = 0          # portfolios whose factor risk backfilled + fit
    attribution_computed: int = 0   # portfolios whose sector attribution backfilled + ran
    earnings_refreshed: int = 0     # securities whose next earnings date refreshed from the spine
    universe_published: bool = False  # held-ticker universe published to the data spine


def daily_refresh(session: Session, *, today: date | None = None) -> RefreshResult:
    """Refresh prices + record a NAV snapshot for every portfolio in the DB.

    Per portfolio: fetch the latest close for each held ticker into the global price
    cache, then snapshot today's NAV (skipped, not fabricated, when nothing is priceable).
    Returns aggregate counts for logging.
    """
    today = today or date.today()

    # Refresh the Reference Rate showcase from the engine's published artifact BEFORE the
    # per-portfolio loop, so its holdings are current when today's NAV snapshot is recorded.
    # Gated on the S3 data-spine toggle (the artifact lives in that bucket) and best-effort:
    # a missing artifact / read failure WARNs and leaves the last-good showcase intact.
    if settings.demo_enabled and settings.market_data_sync_enabled:
        from api.services import demo

        try:
            synced = demo.sync_reference_holdings(session)
            logger.info("reference-rate sync: %s", "updated" if synced else "no artifact (kept last-good)")
        except Exception as e:  # noqa: BLE001 - best-effort showcase sync; never fatal
            logger.warning("reference-rate sync failed (non-fatal): %s", e)
            session.rollback()

    def _best_effort(label: str, portfolio_id, fn):
        """Run a derived backfill; on failure log a WARN and roll back its partial work
        so the next portfolio (and the already-committed price refresh) is unaffected.
        Returns the callable's result, or None if it raised."""
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 — best-effort derived analytics, never fatal
            logger.warning("portfolio %s: %s backfill failed (non-fatal): %s", portfolio_id, label, e)
            session.rollback()
            return None

    portfolios = session.scalars(select(models.Portfolio)).all()
    total_symbols = total_updated = total_snaps = total_fx = 0
    total_recon = total_risk = total_attr = total_acct_snaps = total_earnings = 0
    for p in portfolios:
        held = analytics.holdings(session, p.tenant_id, p.id)
        symbols = [h.ticker for h in held if h.ticker]
        updated = price_service.refresh_latest_prices(session, symbols) if symbols else 0
        # Refresh FX for every non-base currency held, so foreign positions convert into
        # the base-currency NAV instead of being dropped from the total.
        base = p.base_currency or "USD"
        currencies = sorted({h.currency for h in held if h.currency and h.currency != base})
        fx_updated = fx.refresh_fx_rates(session, currencies, base=base) if currencies else 0
        # Backfill FX history over the foreign-transaction span so realized/dividend income
        # converts at its as-of-date rate.
        txn_ccys, earliest = analytics.foreign_transaction_currencies(session, p.tenant_id, p.id, base=base)
        if txn_ccys and earliest is not None:
            fx_updated += fx.backfill_fx_rates(session, txn_ccys, earliest, today, base=base)
        # Reconstruct the historical NAV series from the lot timeline FIRST (best-effort),
        # then record TODAY from live positions — so the authoritative live value (complete
        # even for non-lot holdings) overwrites today's reconstructed point rather than the
        # reverse (the reconstruct-clobbers-live bug, metron-ops#74).
        recon = _best_effort(
            "performance", p.id,
            lambda p=p: performance.reconstruct_snapshots(session, p.tenant_id, p.id, today=today),
        )
        snap = performance.record_snapshot(session, p.tenant_id, p.id, today=today)
        # Per-account NAV snapshots — additive, best-effort (a failure here never costs the
        # portfolio snapshot). Starts the per-account history that can't be reconstructed.
        acct_snaps = _best_effort(
            "account-snapshots", p.id,
            lambda p=p: performance.record_account_snapshots(session, p.tenant_id, p.id, today=today),
        )
        # Overnight/intraday/day decomposition for the day (metron-ops#87) — additive,
        # best-effort; records the split from the intraday spine so its history accrues.
        _best_effort(
            "intraday-legs", p.id,
            lambda p=p: performance.record_intraday_legs(
                session, p.tenant_id, p.id, today=today, feed_entitled=settings.feed_entitled
            ),
        )
        risk_summary = _best_effort(
            "risk", p.id,
            lambda p=p: risk.compute_risk(session, p.tenant_id, p.id, today=today, do_backfill=True),
        )
        attr_summary = _best_effort(
            "attribution", p.id,
            lambda p=p: attribution.compute_attribution(session, p.tenant_id, p.id, today=today, do_backfill=True),
        )
        # Earnings dates for the Calendar page — pulled from the data spine into
        # securities.next_earnings_date. Only the manual "Refresh earnings" button did this
        # before, so the Calendar stayed blank on an untouched deploy (metron-ops#76);
        # auto-refreshing it here populates it overnight like risk/attribution.
        earnings = _best_effort(
            "earnings", p.id,
            lambda p=p, syms=symbols: calendar_svc.refresh_earnings(session, syms),
        )

        total_symbols += len(symbols)
        total_updated += updated
        total_fx += fx_updated
        total_snaps += 1 if snap is not None else 0
        total_acct_snaps += acct_snaps or 0
        total_recon += recon or 0
        total_risk += 1 if (risk_summary is not None and risk_summary.computable) else 0
        total_attr += 1 if (attr_summary is not None and attr_summary.computable) else 0
        total_earnings += earnings or 0
        logger.info(
            "portfolio %s: %d symbols, %d prices, %d fx, snapshot=%s, reconstructed=%s, risk=%s, attribution=%s, earnings=%s",
            p.id, len(symbols), updated, fx_updated, snap is not None,
            recon or 0,
            risk_summary.computable if risk_summary is not None else False,
            attr_summary.computable if attr_summary is not None else False,
            earnings or 0,
        )
    # Publish the held-ticker universe to the data spine so `alpha-engine-data` knows
    # which EOD closes + FX pairs to pull. Best-effort: a failure here WARNs and never
    # costs the price refresh + NAV snapshots, which have already committed. Recording
    # surface = this WARN (+ the data-spine freshness monitor once ARTIFACT_REGISTRY
    # tracks it). Gated off by default so dev/tests never reach S3.
    universe_published = False
    if settings.market_data_sync_enabled:
        try:
            data_spine.publish_holdings_universe(session, today=today)
            universe_published = True
        except Exception as e:  # noqa: BLE001 - best-effort secondary path; never fatal
            logger.warning("holdings-universe publish failed (non-fatal): %s", e)

    return RefreshResult(
        portfolios=len(portfolios),
        symbols=total_symbols,
        prices_updated=total_updated,
        snapshots_recorded=total_snaps,
        fx_rates_updated=total_fx,
        snapshots_reconstructed=total_recon,
        account_snapshots_recorded=total_acct_snaps,
        risk_computed=total_risk,
        attribution_computed=total_attr,
        earnings_refreshed=total_earnings,
        universe_published=universe_published,
    )


def mark_unlisted(session: Session, symbol: str, *, unlisted: bool = True) -> int:
    """Flag every Security row for ``symbol`` as having no public listing (or undo).

    An unlisted instrument (e.g. a 401(k) plan-level CIT like PCKM) is priced from the
    broker snapshot, never yfinance — flagging it drops it from the published holdings
    universe so the data spine stops asking yfinance for it (config#1029). Matches on
    ``symbol`` OR ``yf_symbol`` (case-insensitive), idempotent, returns rows updated.
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        raise ValueError("symbol is required")
    rows = session.scalars(
        select(models.Security).where(
            (models.Security.symbol == sym) | (models.Security.yf_symbol == sym)
        )
    ).all()
    for row in rows:
        row.yf_unlisted = unlisted
    session.commit()
    logger.info("mark_unlisted: %s → yf_unlisted=%s (%d security row(s))", sym, unlisted, len(rows))
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m api.maintenance", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("daily-refresh", help="refresh prices + record NAV snapshots for all portfolios")
    p_unl = sub.add_parser(
        "mark-unlisted",
        help="flag a security as having no public listing (broker-snapshot-priced; "
        "excluded from the published holdings universe) and republish the universe",
    )
    p_unl.add_argument("symbol", help="ticker as the broker reports it (or its yf_symbol)")
    p_unl.add_argument("--undo", action="store_true", help="clear the flag instead of setting it")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.cmd == "mark-unlisted":
        create_all()  # additive auto-ALTER brings an existing SQLite DB up to the model
        session = SessionLocal()
        try:
            n = mark_unlisted(session, args.symbol, unlisted=not args.undo)
            if n == 0:
                logger.warning("mark-unlisted: no security row matched %r — nothing changed", args.symbol)
                return 1
            # Take effect immediately rather than waiting for the next daily-refresh.
            # Best-effort per the daily-refresh posture: the flag has already
            # committed (the primary deliverable); a failed publish is recorded
            # here as a WARN and the next daily-refresh republishes anyway.
            if settings.market_data_sync_enabled:
                try:
                    data_spine.publish_holdings_universe(session)
                except Exception as e:
                    logger.warning("mark-unlisted: flag saved but universe republish failed (%s) — "
                                   "next daily-refresh will republish", e)
        finally:
            session.close()
        return 0
    if args.cmd == "daily-refresh":
        create_all()  # ensure the personal/dev SQLite schema exists before operating
        session = SessionLocal()
        try:
            r = daily_refresh(session)
        finally:
            session.close()
        logger.info(
            "daily-refresh done: %d portfolios, %d symbols, %d prices, %d snapshots, "
            "%d account-snapshots, %d reconstructed, %d risk, %d attribution, universe_published=%s",
            r.portfolios,
            r.symbols,
            r.prices_updated,
            r.snapshots_recorded,
            r.account_snapshots_recorded,
            r.snapshots_reconstructed,
            r.risk_computed,
            r.attribution_computed,
            r.universe_published,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
