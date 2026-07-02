// WatchlistPanel — add/remove via server actions + the held/watching status badge and
// the surfaced error on a failed action (never silently swallowed).

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

const mocks = vi.hoisted(() => ({
  refresh: vi.fn(),
  addWatchlistAction: vi.fn(async (_pid: string, _sym: string, _note?: string) => ({ ok: true, message: "" })),
  removeWatchlistAction: vi.fn(async (_pid: string, _sym: string) => ({ ok: true, message: "" })),
}));

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: mocks.refresh }) }));
vi.mock("@/app/portfolios/[id]/actions", () => ({
  addWatchlistAction: mocks.addWatchlistAction,
  removeWatchlistAction: mocks.removeWatchlistAction,
}));

import { WatchlistPanel } from "@/components/watchlist-panel";
import type { WatchlistEntry } from "@/lib/api";

// Metric fields (metron-ops#121) are irrelevant to this component's read-only reference-data
// panel — every entry gets the same all-null block, only symbol/name/sector/held/note vary.
const EMPTY_METRICS = {
  country: null,
  market_cap: null,
  pe: null,
  fwd_pe: null,
  pb: null,
  ps: null,
  ev_ebitda: null,
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
  attractiveness_valuation: null,
  attractiveness_upside: null,
  attractiveness_rating: null,
  attractiveness_revision: null,
  attractiveness_sentiment: null,
} satisfies Omit<WatchlistEntry, "symbol" | "name" | "sector" | "next_earnings_date" | "held" | "note">;

const entry = (symbol: string, held: boolean): WatchlistEntry => ({
  symbol,
  name: `${symbol} Inc`,
  sector: "Tech",
  next_earnings_date: null,
  held,
  note: null,
  ...EMPTY_METRICS,
});

beforeEach(() => {
  mocks.refresh.mockClear();
  mocks.addWatchlistAction.mockClear();
  mocks.removeWatchlistAction.mockClear();
});

describe("WatchlistPanel", () => {
  it("shows held vs watching status", () => {
    render(<WatchlistPanel portfolioId="p" entries={[entry("MSFT", true), entry("TSLA", false)]} />);
    expect(screen.getByText("Held")).toBeInTheDocument();
    expect(screen.getByText("Watching")).toBeInTheDocument();
  });

  it("adds a ticker (normalized upper-case) then refreshes", async () => {
    render(<WatchlistPanel portfolioId="p" entries={[]} />);
    fireEvent.change(screen.getByLabelText("Ticker to add"), { target: { value: "nvda" } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    await waitFor(() => expect(mocks.addWatchlistAction).toHaveBeenCalledWith("p", "NVDA", ""));
    await waitFor(() => expect(mocks.refresh).toHaveBeenCalled());
  });

  it("removes a ticker", async () => {
    render(<WatchlistPanel portfolioId="p" entries={[entry("AAPL", false)]} />);
    fireEvent.click(screen.getByLabelText("Remove AAPL"));
    await waitFor(() => expect(mocks.removeWatchlistAction).toHaveBeenCalledWith("p", "AAPL"));
  });

  it("surfaces a failed add inline and does not refresh", async () => {
    mocks.addWatchlistAction.mockResolvedValueOnce({ ok: false, message: "The demo portfolio is read-only." });
    render(<WatchlistPanel portfolioId="p" entries={[]} />);
    fireEvent.change(screen.getByLabelText("Ticker to add"), { target: { value: "NVDA" } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    await waitFor(() => expect(screen.getByText("The demo portfolio is read-only.")).toBeInTheDocument());
    expect(mocks.refresh).not.toHaveBeenCalled();
  });
});
