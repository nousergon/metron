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

const entry = (symbol: string, held: boolean): WatchlistEntry => ({
  symbol,
  name: `${symbol} Inc`,
  sector: "Tech",
  next_earnings_date: null,
  held,
  note: null,
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
