// Day-change columns (Brian, 2026-07-08): the Day $ / Day % pair lives in the VALUE band
// so the Overview landing shows each position's session move; the Returns band shows Day
// with its overnight/intraday legs as explicit subordinate columns (not a hover tooltip).

import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), refresh: vi.fn() }),
  usePathname: () => "/portfolios/p1/holdings",
  useSearchParams: () => new URLSearchParams(""),
}));
vi.mock("@/app/portfolios/[id]/actions", () => ({
  saveHoldingsViewAction: vi.fn(),
  setSecurityLabelAction: vi.fn(),
  setSecurityClassificationAction: vi.fn(),
}));

import { HoldingsView } from "@/components/holdings-view";
import type { Holding } from "@/lib/api";

const holding = {
  ticker: "AAPL",
  quantity: 10,
  avg_cost: 100,
  cost_basis: 1000,
  currency: "USD",
  fx_rate: 1,
  last_price: 130,
  last_price_date: "2026-07-08",
  market_value_local: 1300,
  cost_basis_base: 1000,
  market_value: 1300,
  unrealized_gain: 250,
  unrealized_pct: 0.25,
  security_type: "equity",
  account_id: null,
  account_label: null,
  sector: "Technology",
  country: "United States",
  overnight_pct: 0.1,
  intraday_pct: 0.181818,
  day_pct: 0.3,
  day_change: 300, // 1300 × 0.3/1.3
} as Holding;

describe("Holdings day-change columns", () => {
  it("the Overview landing (Value band) carries the Day $ / Day % pair", () => {
    render(<HoldingsView holdings={[holding]} baseCurrency="USD" priced medians={null} portfolioId="p1" />);
    expect(screen.getByText("Day $")).toBeInTheDocument();
    expect(screen.getByText("Day %")).toBeInTheDocument();
    expect(screen.getAllByText("$300")).toHaveLength(2); // the Day $ cell + its footer total
  });

  it("the Returns preset shows Day with its overnight/intraday legs as columns", () => {
    render(<HoldingsView holdings={[holding]} baseCurrency="USD" priced medians={null} portfolioId="p1" />);
    fireEvent.click(screen.getByRole("button", { name: "Returns" }));
    expect(screen.getByText("Day")).toBeInTheDocument();
    expect(screen.getByText("· O/N")).toBeInTheDocument();
    expect(screen.getByText("· Intra")).toBeInTheDocument();
    expect(screen.getByText("10.0%")).toBeInTheDocument(); // overnight leg
    expect(screen.getByText("18.2%")).toBeInTheDocument(); // intraday leg
  });

  it("renders em-dashes, not zeros, in the settled regime (no day legs)", () => {
    const settled = { ...holding, overnight_pct: null, intraday_pct: null, day_pct: null, day_change: null } as Holding;
    render(<HoldingsView holdings={[settled]} baseCurrency="USD" priced medians={null} portfolioId="p1" />);
    // Day $ and Day % cells (and the Day $ footer) fall back to "—".
    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(2);
    expect(screen.queryByText("$0")).not.toBeInTheDocument();
  });
});
