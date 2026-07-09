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
from api.services import analytics, attribution, broker_sync, data_spine, fx, performance, risk
from api.services import calendar as calendar_svc
from api.services import prices as price_service
from api.services.demo import REFERENCE_PORTFOLIO_ID
from portfolio_analytics.prices import fetch_latest_closes

logger = logging.getLogger(__name__)


def _market_closes_published(today: date, *, source=None) -> bool:
    """True when the spine has published the EOD close for ``today``'s OWN session — the
    precondition for stamping a NAV snapshot under ``today`` without back-dating stale
    prices. SPY is the market-freshness proxy (always in the spine, always priceable).

    Why this gate exists: ``record_snapshot`` values holdings on whatever close is cached
    and stamps it ``today``. If a refresh runs before the spine has published today's
    close (a scheduled fire that lands ahead of the EOD pipeline, or any pre-close run),
    it would record YESTERDAY's prices under today's date — the "Today tile shows a flat /
    wrong move" failure. Deferring until the close prints keeps the tile honestly on the
    prior session ("as of <date>") until a true ``today`` value exists.

    Scope: a trading weekday only. Weekends/holidays have no new session, so the gate is a
    no-op there and the existing carry-forward snapshot behaviour is unchanged. When SPY
    can't be fetched at all, default True — never block the daily snapshot on a transient
    spine hiccup (the existing 'skipped when nothing is priceable' guard still applies)."""
    if today.weekday() >= 5:  # Sat/Sun — no new close to wait on; carry-forward unchanged.
        return True
    try:
        spy = fetch_latest_closes(["SPY"], source=source).get("SPY")
    except Exception as e:  # noqa: BLE001 — a probe failure must never block the daily snapshot
        logger.warning("close-freshness probe failed (non-fatal, recording anyway): %s", e)
        return True
    return spy is None or spy.bar_date >= today


@dataclass
class RefreshResult:
    portfolios: int
    symbols: int
    prices_updated: int
    snapshots_recorded: int
    fx_rates_updated: int = 0
    snapshots_reconstructed: int = 0
    snapshots_reconciled: int = 0  # provisional snapshots restated with struck fund NAVs
    account_snapshots_recorded: int = 0  # per-account NAV snapshots written this run
    snapshots_deferred: int = 0     # portfolios whose snapshot was deferred (today's close unpublished)
    risk_computed: int = 0          # portfolios whose factor risk backfilled + fit
    attribution_computed: int = 0   # portfolios whose sector attribution backfilled + ran
    earnings_refreshed: int = 0     # securities whose next earnings date refreshed from the spine
    universe_published: bool = False  # held-ticker universe published to the data spine
    watchlist_universe_published: bool = False  # watchlist-only-ticker universe published (metron-ops#132)
    broker_flex_synced: int = 0     # portfolios whose IBKR Flex-sourced accounts were re-synced (metron-ops#150)
    broker_snaptrade_synced: int = 0  # portfolios whose SnapTrade-sourced accounts were re-synced (metron-ops#150)


def daily_refresh(session: Session, *, today: date | None = None) -> RefreshResult:
    """Refresh prices + record a NAV snapshot for every portfolio in the DB.

    Per portfolio: fetch the latest close for each held ticker into the global price
    cache, then snapshot today's NAV (skipped, not fabricated, when nothing is priceable).
    Returns aggregate counts for logging.

    EXCEPTION — the Reference Rate showcase (``REFERENCE_PORTFOLIO_ID``): its NAV series
    has exactly one authoritative source, the engine's published ``nav_history`` artifact
    (seeded/upserted by ``demo.sync_reference_holdings`` earlier in this function). The
    generic per-portfolio NAV writers below (``record_snapshot``, ``record_account_snapshots``,
    ``reconstruct_snapshots``, ``reconcile_snapshots``) each independently RE-DERIVE NAV from
    Metron's own price/FX cache — for every other portfolio that's the whole point, but for
    the reference portfolio it's a second, non-reconciled source of truth racing the first to
    write the identical ``NavSnapshot`` row, silently diverging by a few percent depending on
    which writer finishes last (metron-ops#141). So this portfolio is skipped in the loop
    below; its NAV/holdings pricing display (live "Total value" tiles etc., which are a
    request-time re-valuation, not a persisted series) is unaffected and intentionally still
    shows Metron's own live pricing.
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

    # Refresh the neutral research-intel snapshot from the crucible-research artifact
    # (config#1499 Phase 1). Global, DB-free last-good cache; same S3-toggle gate + best-
    # effort posture as the showcase above — a missing/unreadable artifact keeps last-good.
    if settings.market_data_sync_enabled:
        from portfolio_analytics.ingestion import research_intel_store

        try:
            updated = research_intel_store.sync_research_intel()
            logger.info("research-intel sync: %s", "updated" if updated else "no artifact (kept last-good)")
        except Exception as e:  # noqa: BLE001 - best-effort intel sync; never fatal
            logger.warning("research-intel sync failed (non-fatal): %s", e)

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
    total_reconciled = 0
    total_flex_synced = total_snaptrade_synced = 0

    # Freshness gate: only stamp a ``today`` NAV snapshot once today's own close has
    # published in the spine. This lets the refresh fire SOON after the close (instead of
    # waiting hours for a safely-late single run) without ever back-dating stale prices —
    # an early fire defers the snapshot, a later fire records it. FX / reconcile /
    # reconstruct / risk / attribution / earnings are not close-of-today-dependent and run
    # regardless. Market-wide, so computed once (not per portfolio).
    closes_published = _market_closes_published(today)
    if not closes_published:
        logger.warning(
            "daily-refresh: %s close not yet published in the spine — deferring NAV "
            "snapshots this run (a later fire records them once today's close prints)",
            today,
        )

    for p in portfolios:
        is_reference_rate = p.id == REFERENCE_PORTFOLIO_ID
        # Re-sync broker-reported positions BEFORE computing holdings, so a real trade at
        # the broker (a buy/sell since the last sync) is reflected in today's price refresh
        # + NAV snapshot instead of waiting on a manual "Sync" click — the fix for
        # metron-ops#150 (a sold PLTR position still showed its pre-sale value days later).
        # Best-effort and independent per broker so a Flex outage never blocks the
        # SnapTrade sync (or vice versa) for the same portfolio; each is itself a no-op
        # (returns None, not an error) for a portfolio that never connected that broker.
        # Skipped for the reference-rate showcase, which has no real brokerage attached.
        flex_synced = None if is_reference_rate else _best_effort(
            "broker-sync-flex", p.id,
            lambda p=p: broker_sync.sync_flex_for_portfolio(session, p),
        )
        snaptrade_synced = None if is_reference_rate else _best_effort(
            "broker-sync-snaptrade", p.id,
            lambda p=p: broker_sync.sync_snaptrade_for_portfolio(session, p),
        )
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

        # The Reference Rate showcase's NavSnapshot series is sole-sourced from the engine's
        # artifact (demo.sync_reference_holdings, above) — see the single-source-of-truth
        # note on this function's docstring (metron-ops#141). None of the generic NAV
        # writers below may touch it, or they'd re-derive + clobber it from Metron's own
        # price/FX cache. (``is_reference_rate`` is computed at the top of this loop, before
        # the broker-position re-sync above.)

        # Reconcile provisional snapshots BEFORE recording today's — the price refresh just
        # above cached the prior session's now-struck mutual-fund NAVs, so this restates
        # yesterday's stale-fund snapshot with the real value before today's (again
        # provisional) snapshot is recorded. Best-effort; a failure never costs the snapshot.
        reconciled = None if is_reference_rate else _best_effort(
            "reconcile", p.id,
            lambda p=p: performance.reconcile_snapshots(session, p.tenant_id, p.id, today=today),
        )
        # Reconstruct the historical NAV series from the lot timeline FIRST (best-effort),
        # then record TODAY from live positions — so the authoritative live value (complete
        # even for non-lot holdings) overwrites today's reconstructed point rather than the
        # reverse (the reconstruct-clobbers-live bug, metron-ops#74).
        recon = None if is_reference_rate else _best_effort(
            "performance", p.id,
            lambda p=p: performance.reconstruct_snapshots(session, p.tenant_id, p.id, today=today),
        )
        snap = (
            performance.record_snapshot(session, p.tenant_id, p.id, today=today)
            if closes_published and not is_reference_rate
            else None
        )
        # Per-account NAV snapshots — additive, best-effort (a failure here never costs the
        # portfolio snapshot). Starts the per-account history that can't be reconstructed.
        # Gated on the same close-freshness check so per-account history doesn't back-date.
        acct_snaps = (
            _best_effort(
                "account-snapshots", p.id,
                lambda p=p: performance.record_account_snapshots(session, p.tenant_id, p.id, today=today),
            )
            if closes_published and not is_reference_rate
            else None
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
        total_reconciled += reconciled or 0
        total_risk += 1 if (risk_summary is not None and risk_summary.computable) else 0
        total_attr += 1 if (attr_summary is not None and attr_summary.computable) else 0
        total_earnings += earnings or 0
        total_flex_synced += 1 if flex_synced is not None else 0
        total_snaptrade_synced += 1 if snaptrade_synced is not None else 0
        logger.info(
            "portfolio %s: %d symbols, %d prices, %d fx, snapshot=%s, reconstructed=%s, reconciled=%s, risk=%s, attribution=%s, earnings=%s, flex_synced=%s, snaptrade_synced=%s",
            p.id, len(symbols), updated, fx_updated, snap is not None,
            recon or 0, reconciled or 0,
            risk_summary.computable if risk_summary is not None else False,
            attr_summary.computable if attr_summary is not None else False,
            earnings or 0,
            flex_synced is not None,
            snaptrade_synced is not None,
        )
    # Publish the held-ticker universe to the data spine so `alpha-engine-data` knows
    # which EOD closes + FX pairs to pull. Best-effort: a failure here WARNs and never
    # costs the price refresh + NAV snapshots, which have already committed. Recording
    # surface = this WARN (+ the data-spine freshness monitor once ARTIFACT_REGISTRY
    # tracks it). Gated off by default so dev/tests never reach S3.
    universe_published = False
    watchlist_universe_published = False
    if settings.market_data_sync_enabled:
        try:
            data_spine.publish_holdings_universe(session, today=today)
            universe_published = True
        except Exception as e:  # noqa: BLE001 - best-effort secondary path; never fatal
            logger.warning("holdings-universe publish failed (non-fatal): %s", e)
        # Watchlist-only-ticker universe (metron-ops#132) — same best-effort posture, its
        # own try/except so a failure here never blocks the held-universe publish above.
        try:
            data_spine.publish_watchlist_universe(session, today=today)
            watchlist_universe_published = True
        except Exception as e:  # noqa: BLE001 - best-effort secondary path; never fatal
            logger.warning("watchlist-universe publish failed (non-fatal): %s", e)

    return RefreshResult(
        portfolios=len(portfolios),
        symbols=total_symbols,
        prices_updated=total_updated,
        snapshots_recorded=total_snaps,
        fx_rates_updated=total_fx,
        snapshots_reconstructed=total_recon,
        snapshots_reconciled=total_reconciled,
        account_snapshots_recorded=total_acct_snaps,
        snapshots_deferred=(len(portfolios) if not closes_published else 0),
        risk_computed=total_risk,
        attribution_computed=total_attr,
        earnings_refreshed=total_earnings,
        universe_published=universe_published,
        watchlist_universe_published=watchlist_universe_published,
        broker_flex_synced=total_flex_synced,
        broker_snaptrade_synced=total_snaptrade_synced,
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


def flex_sync_all(session: Session) -> int:
    """Re-sync IBKR Flex broker positions for every non-reference portfolio.
    A lighter pre-market variant of the daily-refresh: Flex sync only, no price
    refresh / NAV snapshot / derived analytics. Intended for a pre-market systemd
    timer so broker-adjusted positions (ADR ratio changes, stock splits, trades)
    are reflected before the trading day begins — the valuation-layer ADR scale
    cross-check in ``_apply_valuation`` then has an authoritative broker total to
    compare against. Best-effort per portfolio (WARN + rollback on failure)."""
    portfolios = list(
        session.scalars(
            select(models.Portfolio).where(models.Portfolio.id != REFERENCE_PORTFOLIO_ID)
        ).all()
    )
    synced = 0
    for p in portfolios:
        try:
            result = broker_sync.sync_flex_for_portfolio(session, p)
            if result is not None:
                synced += 1
        except Exception:  # noqa: BLE001 - best-effort per portfolio; never fatal
            logger.warning("flex-sync failed for portfolio %s — rolling back", p.id, exc_info=True)
            session.rollback()
        try:
            result = broker_sync.sync_snaptrade_for_portfolio(session, p)
            if result is not None:
                synced += 1
        except Exception:  # noqa: BLE001
            logger.warning("snaptrade-sync failed for portfolio %s — rolling back", p.id, exc_info=True)
            session.rollback()
    logger.info(
        "flex-sync done: %d portfolios, %d broker-sources synced",
        len(portfolios), synced,
    )
    return synced


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m api.maintenance", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("daily-refresh", help="refresh prices + record NAV snapshots for all portfolios")
    sub.add_parser("flex-sync", help="re-sync IBKR Flex / SnapTrade broker positions only (pre-market timer)")
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
            "%d deferred, %d account-snapshots, %d reconstructed, %d risk, %d attribution, "
            "universe_published=%s, %d flex-synced, %d snaptrade-synced",
            r.portfolios,
            r.symbols,
            r.prices_updated,
            r.snapshots_recorded,
            r.snapshots_deferred,
            r.account_snapshots_recorded,
            r.snapshots_reconstructed,
            r.risk_computed,
            r.attribution_computed,
            r.universe_published,
            r.broker_flex_synced,
            r.broker_snaptrade_synced,
        )
        return 0
    if args.cmd == "flex-sync":
        create_all()
        session = SessionLocal()
        try:
            flex_sync_all(session)
        finally:
            session.close()
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
