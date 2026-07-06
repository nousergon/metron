"use client";

// Watchlist comparison table (metron-ops#121) — add tickers you don't hold and sort them
// alongside the Holdings metrics (valuation/fundamentals/technicals/consensus/attractiveness
// — fundamentals now includes balance-sheet/debt metrics, metron-ops#140) for side-by-side
// comparison. Renders through the SAME HoldingsTable column/band/sort machinery as real
// positions, restricted to the comparison-relevant bands (no Position/Value/Returns — a
// watchlist entry has no position; `showMarketValue={false}` also drops the frozen Market
// Value spine column, which would otherwise be all "—"). Mutations never touch NAV/performance
// entries.
//
// entries carry no quantity/cost/market-value fields at all (see WatchlistEntry in
// lib/api.ts) — the shell Holding built below hard-codes those to zero/null so a glance at
// the JSON can never be misread as a real position.

import { useMemo, useState, useTransition, type FormEvent } from "react";
import { useRouter } from "next/navigation";
import type { Holding, WatchlistEntry } from "@/lib/api";
import { Empty } from "@/components/ui";
import { HoldingsTable, type ColumnBand } from "@/components/holdings-table";
import { addWatchlistAction, removeWatchlistAction } from "@/app/portfolios/[id]/actions";

const WATCHLIST_BANDS: ColumnBand[] = [
  "Class",
  "Attractiveness",
  "Valuation",
  "Fundamentals",
  "Technicals",
  "Consensus",
];

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
    ytd_pct: null,
    ltm_pct: null,
    sector: e.sector,
    country: e.country,
    market_cap: e.market_cap,
    pe: e.pe,
    fwd_pe: e.fwd_pe,
    pb: e.pb,
    ps: e.ps,
    ev_ebitda: e.ev_ebitda,
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
    attractiveness_valuation: e.attractiveness_valuation,
    attractiveness_upside: e.attractiveness_upside,
    attractiveness_rating: e.attractiveness_rating,
    attractiveness_revision: e.attractiveness_revision,
    attractiveness_sentiment: e.attractiveness_sentiment,
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
  const router = useRouter();
  const [symbol, setSymbol] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pending, start] = useTransition();

  const shells = useMemo(() => entries.map(toShellHolding), [entries]);
  const alsoHeld = useMemo(() => entries.filter((e) => e.held).map((e) => e.symbol), [entries]);

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
      router.refresh();
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
      router.refresh();
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
            visibleBands={WATCHLIST_BANDS}
            showTotals={false}
            showMarketValue={false}
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
