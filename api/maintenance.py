"""Operational maintenance jobs for a running Metron deploy.

``daily-refresh`` keeps every portfolio current: re-fetch EOD prices for held tickers
and record today's NAV snapshot (the forward-recorded performance series). Run it once
daily from a scheduler (systemd timer / cron) so the personal setup stays a hands-off
daily driver — no manual "refresh prices" click needed, and the NAV-vs-SPY history
accrues on its own. Idempotent per day (re-running updates the same day's bars/snapshot).

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

from api.db import models
from api.db.session import SessionLocal, create_all
from api.services import analytics, fx, performance
from api.services import prices as price_service

logger = logging.getLogger(__name__)


@dataclass
class RefreshResult:
    portfolios: int
    symbols: int
    prices_updated: int
    snapshots_recorded: int
    fx_rates_updated: int = 0


def daily_refresh(session: Session, *, today: date | None = None) -> RefreshResult:
    """Refresh prices + record a NAV snapshot for every portfolio in the DB.

    Per portfolio: fetch the latest close for each held ticker into the global price
    cache, then snapshot today's NAV (skipped, not fabricated, when nothing is priceable).
    Returns aggregate counts for logging.
    """
    today = today or date.today()
    portfolios = session.scalars(select(models.Portfolio)).all()
    total_symbols = total_updated = total_snaps = total_fx = 0
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
        snap = performance.record_snapshot(session, p.tenant_id, p.id, today=today)
        total_symbols += len(symbols)
        total_updated += updated
        total_fx += fx_updated
        total_snaps += 1 if snap is not None else 0
        logger.info(
            "portfolio %s: %d symbols, %d prices updated, %d fx rates, snapshot=%s",
            p.id, len(symbols), updated, fx_updated, snap is not None,
        )
    return RefreshResult(
        portfolios=len(portfolios),
        symbols=total_symbols,
        prices_updated=total_updated,
        snapshots_recorded=total_snaps,
        fx_rates_updated=total_fx,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m api.maintenance", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("daily-refresh", help="refresh prices + record NAV snapshots for all portfolios")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.cmd == "daily-refresh":
        create_all()  # ensure the personal/dev SQLite schema exists before operating
        session = SessionLocal()
        try:
            r = daily_refresh(session)
        finally:
            session.close()
        logger.info(
            "daily-refresh done: %d portfolios, %d symbols, %d prices updated, %d snapshots recorded",
            r.portfolios,
            r.symbols,
            r.prices_updated,
            r.snapshots_recorded,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
