// Holdings Score band (metron-ops#106, Phase 2): the composite attractiveness headline column
// on HoldingsTable. Feed-gated 0–100 score; "—" off-feed or on a total coverage gap.

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

describe("HoldingsTable Score band", () => {
  it("shows the Score header + the 0–100 attractiveness value", () => {
    render(
      <HoldingsTable
        baseCurrency="USD"
        priced
        holdings={[h("AAPL", { attractiveness: 72.4, attractiveness_coverage: 4 })]}
      />,
    );
    // "Score" appears twice — the group band header + the sortable column header.
    expect(screen.getAllByText("Score").length).toBeGreaterThan(0);
    expect(screen.getByText("72.4")).toBeInTheDocument();
  });

  it("renders — for an attractiveness coverage gap (null), never a fabricated value", () => {
    render(
      <HoldingsTable
        baseCurrency="USD"
        priced
        holdings={[h("ZZZ", { attractiveness: null, attractiveness_coverage: null })]}
      />,
    );
    expect(screen.getAllByText("Score").length).toBeGreaterThan(0); // header still present
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
  });

  it("hides the Score band in the cost-basis-only (unpriced) view", () => {
    render(<HoldingsTable baseCurrency="USD" priced={false} holdings={[h("AAPL", { attractiveness: 72.4 })]} />);
    expect(screen.queryByText("Score")).not.toBeInTheDocument();
  });
});
