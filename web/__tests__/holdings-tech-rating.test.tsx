// Holdings Technicals band — Tech Rating column (metron-ops#294): a signed [-1, +1]
// composite MA + oscillator vote, "Strong Sell" … "Strong Buy". Owner (feed-entitled)
// build only; each cell is null (—) off-feed or on a coverage gap, never fabricated.

import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn() }) }));
vi.mock("@/app/portfolios/[id]/actions", () => ({
  setSecurityLabelAction: vi.fn(),
  setSecurityClassificationAction: vi.fn(),
}));

import { HoldingsTable } from "@/components/holdings-table";
import type { Holding } from "@/lib/api";

const h = (ticker: string, over: Partial<Holding> = {}): Holding =>
  ({
    ticker,
    quantity: 10,
    avg_cost: 100,
    cost_basis: 1000,
    currency: "USD",
    fx_rate: 1,
    last_price: 200,
    last_price_date: "2026-06-26",
    market_value_local: 2000,
    cost_basis_base: 1000,
    market_value: 2000,
    unrealized_gain: 1000,
    unrealized_pct: 1.0,
    security_type: "equity",
    sector: "Technology",
    country: "United States",
    ...over,
  }) as Holding;

describe("HoldingsTable Technicals band — Tech Rating", () => {
  it("shows the rating label in the Technicals band", () => {
    render(
      <HoldingsTable
        baseCurrency="USD"
        priced
        holdings={[
          h("AAPL", {
            tech_rating_score: 0.6,
            tech_rating_label: "Buy",
            tech_rating_basis: "intraday",
          }),
        ]}
      />,
    );
    expect(screen.getByText("Technicals")).toBeInTheDocument();
    expect(screen.getByText("Buy")).toBeInTheDocument();
  });

  it("renders — for a rating coverage gap (null), never a fabricated label", () => {
    render(
      <HoldingsTable
        baseCurrency="USD"
        priced
        holdings={[h("ZZZ", { tech_rating_score: null, tech_rating_label: null })]}
      />,
    );
    expect(screen.queryByText("Buy")).not.toBeInTheDocument();
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
  });

  it("carries the rating's as-of + basis as a per-row hover title (P-28)", () => {
    render(
      <HoldingsTable
        baseCurrency="USD"
        priced
        holdings={[
          h("AAPL", {
            tech_rating_score: 0.6,
            tech_rating_label: "Buy",
            tech_rating_basis: "intraday",
            tech_rating_as_of: "2026-09-14T15:30:00Z",
          }),
        ]}
      />,
    );
    expect(screen.getByText("Buy").title).toBe("Intraday as of 2026-09-14T15:30:00Z");
  });

  it("hides the Technicals band (and the rating with it) in the cost-basis-only (unpriced) view", () => {
    render(
      <HoldingsTable
        baseCurrency="USD"
        priced={false}
        holdings={[h("AAPL", { tech_rating_score: 0.6, tech_rating_label: "Buy" })]}
      />,
    );
    expect(screen.queryByText("Technicals")).not.toBeInTheDocument();
    expect(screen.queryByText("Buy")).not.toBeInTheDocument();
  });
});
