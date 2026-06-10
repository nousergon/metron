"""Upcoming-event sourcing for the Calendar page.

For now: each held ticker's next earnings date, through an injectable source (yfinance
default, free — clean for the public tier; tests inject deterministic dates). FOMC and
macro-release rows are deferred (robodashboard sourced them from alpha-engine's private
S3, which doesn't belong in the free tier).
"""

from portfolio_analytics.calendar.yfinance_source import EarningsSource, fetch_earnings_dates

__all__ = ["EarningsSource", "fetch_earnings_dates"]
