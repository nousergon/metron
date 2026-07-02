// Holdings Attractiveness band (metron-ops#106 Phase 2, metron-ops#130): the composite
// attractiveness headline column + its component sub-scores on HoldingsTable. Feed-gated
// 0–100 score / [0,1] sub-scores; "—" off-feed or on a coverage gap.

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

describe("HoldingsTable Attractiveness band", () => {
  it("shows the Attractiveness band header + the 0–100 score + its component sub-scores", () => {
    render(
      <HoldingsTable
        baseCurrency="USD"
        priced
        holdings={[
          h("AAPL", {
            attractiveness: 72.4,
            attractiveness_coverage: 4,
            attractiveness_valuation: 0.61,
            attractiveness_upside: 0.72,
            attractiveness_rating: 0.8,
            attractiveness_revision: null,
            attractiveness_sentiment: 0.55,
          }),
        ]}
      />,
    );
    expect(screen.getAllByText("Attractiveness").length).toBeGreaterThan(0); // band header
    expect(screen.getByText("Score")).toBeInTheDocument(); // headline column label
    expect(screen.getByText("72.4")).toBeInTheDocument();
    expect(screen.getByText("0.61")).toBeInTheDocument();
    expect(screen.getByText("0.72")).toBeInTheDocument();
    expect(screen.getByText("0.80")).toBeInTheDocument();
    expect(screen.getByText("0.55")).toBeInTheDocument();
  });

  it("renders — for an attractiveness coverage gap (null), never a fabricated value", () => {
    render(
      <HoldingsTable
        baseCurrency="USD"
        priced
        holdings={[
          h("ZZZ", {
            attractiveness: null,
            attractiveness_coverage: null,
            attractiveness_valuation: null,
            attractiveness_upside: null,
            attractiveness_rating: null,
            attractiveness_revision: null,
            attractiveness_sentiment: null,
          }),
        ]}
      />,
    );
    expect(screen.getAllByText("Attractiveness").length).toBeGreaterThan(0); // header still present
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
  });

  it("hides the Attractiveness band in the cost-basis-only (unpriced) view", () => {
    render(<HoldingsTable baseCurrency="USD" priced={false} holdings={[h("AAPL", { attractiveness: 72.4 })]} />);
    expect(screen.queryByText("Attractiveness")).not.toBeInTheDocument();
  });
});
