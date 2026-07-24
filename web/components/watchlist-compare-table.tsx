"use client";

// Watchlist comparison table (metron-ops#121) — add tickers you don't hold and sort them
// alongside the Holdings metrics (valuation/fundamentals/technicals/consensus/attractiveness
// — fundamentals now includes balance-sheet/debt metrics, metron-ops#140) for side-by-side
// comparison. Renders through the SAME HoldingsTable column/band/sort machinery as real
// positions. `showMarketValue={false}` drops the frozen Market Value spine column, which
// would otherwise be all "—". Mutations never touch NAV/performance entries.
//
// The visible band set is now SHARED with the Holdings COLUMNS control (metron-ops#121 sync
// fix, column-bands-context.tsx) rather than a hardcoded constant — picking a preset on
// Holdings drives this table too. Because a watchlist entry has no position, `showTotals` and
// `showPositionEconomics` are false: the Position band's Quantity/Avg cost/Cost basis cells
// render "—" instead of the shell's fabricated zero values (see toShellHolding below) when
// the shared selection includes Position — the band header still renders (partial/dashed,
// consistent with how every other feed-gated column already renders "—" for a null value)
// rather than silently dropping the band, which would reintroduce the "selector doesn't do
// what it says" bug in a new form. Every other band (Value/Returns/Class/analytic bands) is
// already null-safe end to end since the shell hard-codes its unavailable fields to null.
//
// entries carry no quantity/cost/market-value fields at all (see WatchlistEntry in
// lib/api.ts) — the shell Holding built below hard-codes those to zero/null so a glance at
// the JSON can never be misread as a real position.

import { useMemo, useState, useTransition, type FormEvent } from "react";
import type { Holding, WatchlistEntry } from "@/lib/api";
import { Empty } from "@/components/ui";
import { HoldingsTable } from "@/components/holdings-table";
import { useColumnBands } from "@/components/column-bands-context";
import { addWatchlistAction, removeWatchlistAction } from "@/app/portfolios/[id]/actions";
import { useWatchlist } from "@/lib/use-watchlist";

function toShellHolding(e: WatchlistEntry): Holding {
  return {
    ticker: e.symbol,
    quantity: 0,
    avg_cost: 0,
    cost_basis: 0,
    currency: "USD",
    fx_rate: 1,
    last_price: null,
    last_price_date: null,
    last_price_stale: false,
    is_estimated: false,
    broker_as_of: null,
    positions_stale: false,
    market_value_local: null,
    cost_basis_base: null,
    market_value: null,
    unrealized_gain: null,
    unrealized_pct: null,
    security_type: "other",
    account_id: null,
    account_label: null,
    user_label: null,
    overnight_pct: null,
    intraday_pct: null,
    day_pct: null,
    day_change: null,
    ytd_pct: null,
    ltm_pct: null,
    sector: e.sector,
    country: e.country,
    market_cap: e.market_cap,
    pe: e.pe,
    fwd_pe: e.fwd_pe,
    eps: e.eps,
    fwd_eps: e.fwd_eps,
    pb: e.pb,
    book_value_per_share: e.book_value_per_share,
    ps: e.ps,
    revenue_per_share: e.revenue_per_share,
    ev_ebitda: e.ev_ebitda,
    ebitda: e.ebitda,
    enterprise_value: e.enterprise_value,
    peg: e.peg,
    div_yield: e.div_yield,
    rev_growth: e.rev_growth,
    earnings_growth: e.earnings_growth,
    gross_margin: e.gross_margin,
    op_margin: e.op_margin,
    roe: e.roe,
    roa: e.roa,
    beta: e.beta,
    cash: e.cash,
    debt: e.debt,
    net_debt: e.net_debt,
    debt_to_equity: e.debt_to_equity,
    net_debt_to_ebitda: e.net_debt_to_ebitda,
    current_ratio: e.current_ratio,
    quick_ratio: e.quick_ratio,
    fcf: e.fcf,
    rsi_14: e.rsi_14,
    macd_hist: e.macd_hist,
    pct_to_ma_50: e.pct_to_ma_50,
    pct_to_ma_200: e.pct_to_ma_200,
    pct_in_52w_range: e.pct_in_52w_range,
    mom_20d: e.mom_20d,
    consensus_rating: e.consensus_rating,
    consensus_score: e.consensus_score,
    price_target_mean: e.price_target_mean,
    price_target_median: e.price_target_median,
    price_target_upside: e.price_target_upside,
    num_analysts: e.num_analysts,
    news_sentiment: e.news_sentiment,
    news_articles: e.news_articles,
    attractiveness: e.attractiveness,
    attractiveness_coverage: e.attractiveness_coverage,
    attractiveness_quality: e.attractiveness_quality,
    attractiveness_value: e.attractiveness_value,
    attractiveness_momentum: e.attractiveness_momentum,
    attractiveness_growth: e.attractiveness_growth,
    attractiveness_stewardship: e.attractiveness_stewardship,
    attractiveness_defensiveness: e.attractiveness_defensiveness,
  };
}

export function WatchlistCompareTable({
  portfolioId,
  baseCurrency,
  entries,
}: {
  portfolioId: string;
  baseCurrency: string;
  entries: WatchlistEntry[];
}) {
  const [symbol, setSymbol] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pending, start] = useTransition();
  const { bands } = useColumnBands();

  // SWR-backed watchlist (metron-ops#232): SSR data seeds the cache for instant first
  // paint; a mutation calls `mutate()` to revalidate just this cache key instead of
  // `router.refresh()` re-rendering the whole page. The compare-table's visual entries
  // now come from the shared client-side cache so add/remove reflects instantly.
  const { data: watchlistEntries = entries, mutate } = useWatchlist(portfolioId, entries);

  const shells = useMemo(() => watchlistEntries.map(toShellHolding), [watchlistEntries]);
  const alsoHeld = useMemo(() => watchlistEntries.filter((e) => e.held).map((e) => e.symbol), [watchlistEntries]);

  function add(e: FormEvent) {
    e.preventDefault();
    const sym = symbol.trim().toUpperCase();
    if (!sym) return;
    setError(null);
    start(async () => {
      const r = await addWatchlistAction(portfolioId, sym);
      if (!r.ok) {
        setError(r.message);
        return;
      }
      setSymbol("");
      void mutate();
    });
  }

  function remove(ticker: string) {
    setError(null);
    start(async () => {
      const r = await removeWatchlistAction(portfolioId, ticker);
      if (!r.ok) {
        setError(r.message);
        return;
      }
      void mutate();
    });
  }

  return (
    <div>
      <form onSubmit={add} className="flex flex-wrap items-center gap-2">
        <input
          value={symbol}
          onChange={(ev) => setSymbol(ev.target.value)}
          placeholder="Ticker (e.g. NVDA)"
          aria-label="Ticker to add to the watchlist"
          className="w-40 rounded border border-line bg-surface px-2 py-1 text-sm uppercase tabular-nums"
        />
        <button
          type="submit"
          disabled={pending || !symbol.trim()}
          className="rounded border border-line px-3 py-1 text-sm font-medium hover:bg-white/5 disabled:opacity-50"
        >
          Add
        </button>
      </form>
      {error ? <p className="mt-2 text-xs text-negative">{error}</p> : null}

      {entries.length === 0 ? (
        <Empty>
          Your watchlist is empty. Add a ticker above to compare its metrics against your holdings — a watchlist
          entry never affects NAV, performance, or any other portfolio total.
        </Empty>
      ) : (
        <div className="mt-3">
          <HoldingsTable
            holdings={shells}
            baseCurrency={baseCurrency}
            priced
            portfolioId={portfolioId}
            visibleBands={bands}
            showTotals={false}
            showMarketValue={false}
            showPositionEconomics={false}
            onRemove={remove}
          />
          {alsoHeld.length > 0 ? (
            <p className="mt-2 text-xs text-muted">
              Also in your holdings: {alsoHeld.join(", ")} — tracked here for comparison; not double-counted.
            </p>
          ) : null}
        </div>
      )}
    </div>
  );
}
