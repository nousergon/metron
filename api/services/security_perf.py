"""Per-security period returns for the Holdings table — Day (overnight/intraday/day),
YTD, and LTM, per ticker.

- **Day legs** (overnight/intraday/day) reuse ``intraday.today_view`` (the canonical
  prior-close / open / latest decomposition from the yfinance intraday spine). Owner-build
  only (``feed_entitled``); blank otherwise — never fabricated.
- **YTD / LTM** are total returns of the SECURITY from its cached daily closes
  (``prices.close_history_by_symbol``): first close on/after the window start vs the latest
  close. A window with no cached bar reaching back that far is omitted (None), like the
  tearsheet's ``_period_returns`` — never a partial-window number that reads as full.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy.orm import Session

from api.services import intraday
from api.services import prices as price_service
from portfolio_analytics.prices import ClosePoint


@dataclass
class SecurityReturns:
    overnight_pct: float | None = None  # (open − prev_close) / prev_close
    intraday_pct: float | None = None   # (last − open) / open
    day_pct: float | None = None        # (last − prev_close) / prev_close
    ytd_pct: float | None = None        # vs first close on/after Jan 1
    ltm_pct: float | None = None        # vs first close on/after as_of − 1y


def _year_start(as_of: date) -> date:
    return date(as_of.year, 1, 1)


def _year_ago(as_of: date) -> date:
    try:
        return as_of.replace(year=as_of.year - 1)
    except ValueError:  # Feb-29 → Feb-28
        return as_of.replace(year=as_of.year - 1, day=28)


def _window_return(series: list[ClosePoint], start: date) -> float | None:
    """Total return from the first close on/after ``start`` to the latest close — only when
    the cached history actually reaches back to ``start`` (else None, no partial window)."""
    if len(series) < 2 or series[0].bar_date > start:
        return None
    ref = next((p.close for p in series if p.bar_date >= start), None)
    last = series[-1].close
    if ref is None or ref <= 0:
        return None
    return last / ref - 1.0


def per_security_returns(
    session: Session,
    tenant_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    tickers: Collection[str],
    *,
    as_of: date,
    feed_entitled: bool,
    account_ids: Collection[uuid.UUID] | None = None,
    reader=None,
    now: datetime | None = None,
) -> dict[str, SecurityReturns]:
    """``{ticker: SecurityReturns}`` for the given held tickers. Tickers absent from the
    price cache simply carry None YTD/LTM; day legs are None off a feed-entitled build."""
    out: dict[str, SecurityReturns] = {t: SecurityReturns() for t in tickers}
    if not out:
        return out

    hist = price_service.close_history_by_symbol(session, list(out))
    ytd_start, ltm_start = _year_start(as_of), _year_ago(as_of)
    for ticker, sr in out.items():
        series = hist.get(ticker)
        if series:
            sr.ytd_pct = _window_return(series, ytd_start)
            sr.ltm_pct = _window_return(series, ltm_start)

    # Day decomposition from the intraday spine (owner build only).
    if feed_entitled:
        today = intraday.today_view(
            session, tenant_id, portfolio_id,
            feed_entitled=True, account_ids=account_ids, reader=reader, now=now or datetime.now(UTC),
        )
        for row in today.rows:
            sr = out.get(row.ticker)
            if sr is not None:
                sr.overnight_pct = row.overnight_pct
                sr.intraday_pct = row.intraday_pct
                sr.day_pct = row.day_pct
    return out


def enrich_holdings(
    session: Session,
    tenant_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    held: list,
    *,
    as_of: date,
    feed_entitled: bool,
    account_ids: Collection[uuid.UUID] | None = None,
    reader=None,
    now: datetime | None = None,
) -> list:
    """Populate ``overnight_pct`` / ``intraday_pct`` / ``day_pct`` / ``ytd_pct`` /
    ``ltm_pct`` on each ``analytics.Holding`` in place, then return the list."""
    returns = per_security_returns(
        session, tenant_id, portfolio_id, [h.ticker for h in held],
        as_of=as_of, feed_entitled=feed_entitled, account_ids=account_ids, reader=reader, now=now,
    )
    for h in held:
        sr = returns.get(h.ticker)
        if sr is None:
            continue
        h.overnight_pct = sr.overnight_pct
        h.intraday_pct = sr.intraday_pct
        h.day_pct = sr.day_pct
        h.ytd_pct = sr.ytd_pct
        h.ltm_pct = sr.ltm_pct
    return held
