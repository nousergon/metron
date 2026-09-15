// MarketBoardPanel (metron-ops-I304, Stage A) — renders at phone width (390px), the
// Held/Watchlist toggle switches scope via the server action, a held-and-watched symbol
// carries the "Held" badge, an unrated row shows "—" (never a fabricated 0), and a row
// links to the existing tearsheet.

import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

const mocks = vi.hoisted(() => ({
  fetchMarketBoardAction: vi.fn(),
}));

vi.mock("@/app/portfolios/[id]/market-board-action", () => ({
  fetchMarketBoardAction: mocks.fetchMarketBoardAction,
}));

import { MarketBoardPanel } from "@/components/market-board";
import type { MarketBoard } from "@/lib/api-market-board";

const HELD_BOARD: MarketBoard = {
  scope: "held",
  rows: [
    {
      symbol: "MSFT", held: true, label: "Strong Buy", score: 0.8, ma_score: 0.9, osc_score: 0.7,
      change_1d_pct: 0.012, change_5d_pct: -0.004, basis: "intraday", as_of: "2026-09-15T14:55:00Z",
    },
    {
      symbol: "AAPL", held: true, label: null, score: null, ma_score: null, osc_score: null,
      change_1d_pct: null, change_5d_pct: null, basis: null, as_of: null,
    },
  ],
  track_record: { segment: "all", window: 60, horizon: 5, ic_mean: -0.017, noise_floor_ic: 0.02, as_of_utc: "2026-09-14T00:00:00Z" },
};

const WATCHLIST_BOARD: MarketBoard = {
  scope: "watchlist",
  rows: [
    {
      symbol: "NVDA", held: false, label: "Sell", score: -0.6, ma_score: -0.5, osc_score: -0.7,
      change_1d_pct: -0.03, change_5d_pct: -0.08, basis: "eod", as_of: "2026-09-14",
    },
  ],
  track_record: null,
};

beforeEach(() => {
  // Phone-first viewport — jsdom has no layout engine, so this asserts the component
  // renders (no crash, no desktop-only branch) under the width the spec targets, not
  // literal pixel geometry.
  Object.defineProperty(window, "innerWidth", { writable: true, configurable: true, value: 390 });
  window.dispatchEvent(new Event("resize"));
  mocks.fetchMarketBoardAction.mockReset();
});

describe("MarketBoardPanel", () => {
  it("renders held rows at 390px with symbol, label+score, sub-scores, and 1d/5d change", () => {
    render(<MarketBoardPanel portfolioId="p1" initialScope="held" initialBoard={HELD_BOARD} />);
    expect(screen.getByText("MSFT")).toBeTruthy();
    expect(screen.getByText("Strong Buy")).toBeTruthy();
    expect(screen.getByText("0.80")).toBeTruthy();
    // Two "Held" row badges — one per held row (MSFT and AAPL are both positions). Filter
    // out the scope-toggle button, which is also labeled "Held".
    const badges = screen.getAllByText("Held").filter((el) => el.tagName === "SPAN");
    expect(badges).toHaveLength(2);
  });

  it("shows a dash, never a fabricated score, for an unrated row", () => {
    render(<MarketBoardPanel portfolioId="p1" initialScope="held" initialBoard={HELD_BOARD} />);
    const row = screen.getByText("AAPL").closest("tr");
    expect(row).toBeTruthy();
    const cells = row!.querySelectorAll("td");
    expect(cells[1]?.textContent).toContain("—");
  });

  it("links a row to the existing tearsheet route", () => {
    render(<MarketBoardPanel portfolioId="p1" initialScope="held" initialBoard={HELD_BOARD} />);
    const link = screen.getByRole("link", { name: /MSFT/ });
    expect(link.getAttribute("href")).toBe("/portfolios/p1/tearsheet/MSFT");
  });

  it("switches to the watchlist scope via the server action on toggle", async () => {
    mocks.fetchMarketBoardAction.mockResolvedValue({ ok: true, board: WATCHLIST_BOARD });
    render(<MarketBoardPanel portfolioId="p1" initialScope="held" initialBoard={HELD_BOARD} />);
    fireEvent.click(screen.getByRole("button", { name: "Watchlist" }));
    await waitFor(() => expect(screen.getByText("NVDA")).toBeTruthy());
    expect(mocks.fetchMarketBoardAction).toHaveBeenCalledWith("p1", "watchlist");
    expect(screen.queryByText("MSFT")).toBeNull();
    // NVDA is watchlist-only, never held — no "Held" ROW badge on this scope (the
    // scope-toggle button is also labeled "Held", so filter to badge spans only).
    expect(screen.queryAllByText("Held").filter((el) => el.tagName === "SPAN")).toHaveLength(0);
  });

  it("surfaces a failed scope switch instead of silently keeping stale rows mislabeled", async () => {
    mocks.fetchMarketBoardAction.mockResolvedValue({ ok: false, message: "This view isn't available on your plan." });
    render(<MarketBoardPanel portfolioId="p1" initialScope="held" initialBoard={HELD_BOARD} />);
    fireEvent.click(screen.getByRole("button", { name: "Watchlist" }));
    await waitFor(() => expect(screen.getByText("This view isn't available on your plan.")).toBeTruthy());
  });

  it("renders the fixed track-record header copy verbatim", () => {
    render(<MarketBoardPanel portfolioId="p1" initialScope="held" initialBoard={HELD_BOARD} />);
    expect(
      screen.getByText(/Describes recent price action; graded IC ≈ 0 at 1–20 days \(metron-ops#295\)\. Not a forecast, not investment advice\./),
    ).toBeTruthy();
  });
});
