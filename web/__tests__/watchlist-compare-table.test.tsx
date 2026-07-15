// WatchlistCompareTable (metron-ops#121) — the Holdings page's embedded watchlist section:
// add/remove a tracked ticker, and render its Holdings metrics through the same
// HoldingsTable column/band/sort machinery. The visible band set is now SHARED with the
// Holdings COLUMNS control via ColumnBandsProvider (metron-ops#121 sync fix) rather than a
// hardcoded constant — a component rendered without a provider ancestor still gets a working
// default (DEFAULT_VISIBLE_GROUPS = Position + Value, column-bands-context.tsx fallback).

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

const mocks = vi.hoisted(() => ({
  refresh: vi.fn(),
  addWatchlistAction: vi.fn(async (_pid: string, _sym: string) => ({ ok: true, message: "" })),
  removeWatchlistAction: vi.fn(async (_pid: string, _sym: string) => ({ ok: true, message: "" })),
}));

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: mocks.refresh }) }));
vi.mock("@/app/portfolios/[id]/actions", () => ({
  addWatchlistAction: mocks.addWatchlistAction,
  removeWatchlistAction: mocks.removeWatchlistAction,
  setSecurityLabelAction: vi.fn(),
  setSecurityClassificationAction: vi.fn(),
}));

import { WatchlistCompareTable } from "@/components/watchlist-compare-table";
import { ColumnBandsProvider } from "@/components/column-bands-context";
import type { WatchlistEntry } from "@/lib/api";

const EMPTY_METRICS = {
  country: null,
  market_cap: null,
  pe: null,
  fwd_pe: null,
  eps: null,
  fwd_eps: null,
  pb: null,
  book_value_per_share: null,
  ps: null,
  revenue_per_share: null,
  ev_ebitda: null,
  ebitda: null,
  enterprise_value: null,
  peg: null,
  div_yield: null,
  rev_growth: null,
  earnings_growth: null,
  gross_margin: null,
  op_margin: null,
  roe: null,
  roa: null,
  beta: null,
  cash: null,
  debt: null,
  net_debt: null,
  debt_to_equity: null,
  net_debt_to_ebitda: null,
  current_ratio: null,
  quick_ratio: null,
  fcf: null,
  rsi_14: null,
  macd_hist: null,
  pct_to_ma_50: null,
  pct_to_ma_200: null,
  pct_in_52w_range: null,
  mom_20d: null,
  consensus_rating: null,
  consensus_score: null,
  price_target_mean: null,
  price_target_median: null,
  price_target_upside: null,
  num_analysts: null,
  news_sentiment: null,
  news_articles: null,
  attractiveness: null,
  attractiveness_coverage: null,
  attractiveness_quality: null,
  attractiveness_value: null,
  attractiveness_momentum: null,
  attractiveness_growth: null,
  attractiveness_stewardship: null,
  attractiveness_defensiveness: null,
} satisfies Omit<WatchlistEntry, "symbol" | "name" | "sector" | "next_earnings_date" | "held" | "note">;

const entry = (symbol: string, over: Partial<WatchlistEntry> = {}): WatchlistEntry => ({
  symbol,
  name: `${symbol} Inc`,
  sector: "Technology",
  next_earnings_date: null,
  held: false,
  note: null,
  ...EMPTY_METRICS,
  ...over,
});

beforeEach(() => {
  mocks.refresh.mockClear();
  mocks.addWatchlistAction.mockClear();
  mocks.removeWatchlistAction.mockClear();
});

describe("WatchlistCompareTable", () => {
  it("shows an empty state that reassures no NAV/performance impact", () => {
    render(<WatchlistCompareTable portfolioId="p" baseCurrency="USD" entries={[]} />);
    expect(screen.getByText(/never affects NAV, performance/)).toBeInTheDocument();
  });

  it("renders entries with their metrics through the Attractiveness/Valuation bands when those are the shared selection", () => {
    const { container } = render(
      <ColumnBandsProvider initialBands={["Attractiveness", "Valuation"]}>
        <WatchlistCompareTable
          portfolioId="p"
          baseCurrency="USD"
          entries={[entry("NVDA", { fwd_pe: 30.2, attractiveness: 72.5 })]}
        />
      </ColumnBandsProvider>,
    );
    expect(screen.getByText("NVDA")).toBeInTheDocument();
    expect(screen.getByText("30.2×")).toBeInTheDocument();
    expect(screen.getByText("72.5")).toBeInTheDocument();
    // Position band isn't in the shared selection, so it's absent entirely.
    expect(screen.queryByText("Quantity")).not.toBeInTheDocument();
    // Market Value is always suppressed (showMarketValue=false) — no real position.
    expect(screen.queryByText("Market value")).not.toBeInTheDocument();
    // No portfolio-total footer for a comparison-only table.
    expect(container.querySelector("tfoot")).toBeNull();
  });

  it("defaults to the shared Overview selection (Position + Value) when nothing else picked it, same as Holdings' default", () => {
    render(
      <WatchlistCompareTable
        portfolioId="p"
        baseCurrency="USD"
        entries={[entry("NVDA", { fwd_pe: 30.2, attractiveness: 72.5 })]}
      />,
    );
    // Position band now renders (synced to Holdings' DEFAULT_VISIBLE_GROUPS)...
    expect(screen.getByText("Quantity")).toBeInTheDocument();
    // ...but a watchlist entry has no real position, so the Position cells are dashed
    // rather than showing the shell Holding's fabricated zero quantity/cost.
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
    expect(screen.queryByText("0")).not.toBeInTheDocument();
    // Analytic bands not in the default selection don't render.
    expect(screen.queryByText("30.2×")).not.toBeInTheDocument();
  });

  it("syncs to an explicit shared selection that includes a Position/Returns-flavored band without crashing", () => {
    render(
      <ColumnBandsProvider initialBands={["Position", "Returns", "Attractiveness"]}>
        <WatchlistCompareTable
          portfolioId="p"
          baseCurrency="USD"
          entries={[entry("NVDA", { attractiveness: 72.5 })]}
        />
      </ColumnBandsProvider>,
    );
    expect(screen.getByText("NVDA")).toBeInTheDocument();
    expect(screen.getByText("Quantity")).toBeInTheDocument();
    expect(screen.getByText("72.5")).toBeInTheDocument();
    // Returns band is fully null-safe already (the shell hard-codes day/ytd/ltm to null) —
    // renders dashed, not a crash and not silently dropped.
    expect(screen.getByText("YTD")).toBeInTheDocument();
  });

  it("flags an entry that's also a real holding, without double-counting", () => {
    render(
      <WatchlistCompareTable portfolioId="p" baseCurrency="USD" entries={[entry("MSFT", { held: true })]} />,
    );
    expect(screen.getByText(/Also in your holdings: MSFT/)).toBeInTheDocument();
  });

  it("adds a ticker (normalized upper-case) then refreshes", async () => {
    render(<WatchlistCompareTable portfolioId="p" baseCurrency="USD" entries={[]} />);
    fireEvent.change(screen.getByLabelText("Ticker to add to the watchlist"), { target: { value: "nvda" } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    await waitFor(() => expect(mocks.addWatchlistAction).toHaveBeenCalledWith("p", "NVDA"));
    await waitFor(() => expect(mocks.refresh).toHaveBeenCalled());
  });

  it("removes a ticker via the table's Remove column", async () => {
    render(<WatchlistCompareTable portfolioId="p" baseCurrency="USD" entries={[entry("AAPL")]} />);
    fireEvent.click(screen.getByLabelText("Remove AAPL"));
    await waitFor(() => expect(mocks.removeWatchlistAction).toHaveBeenCalledWith("p", "AAPL"));
    await waitFor(() => expect(mocks.refresh).toHaveBeenCalled());
  });

  it("surfaces a failed add inline and does not refresh", async () => {
    mocks.addWatchlistAction.mockResolvedValueOnce({ ok: false, message: "The demo portfolio is read-only." });
    render(<WatchlistCompareTable portfolioId="p" baseCurrency="USD" entries={[]} />);
    fireEvent.change(screen.getByLabelText("Ticker to add to the watchlist"), { target: { value: "NVDA" } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    await waitFor(() => expect(screen.getByText("The demo portfolio is read-only.")).toBeInTheDocument());
    expect(mocks.refresh).not.toHaveBeenCalled();
  });
});
