// Holdings Attractiveness band: SOTA 6-pillar cross-sectional score + pillar columns.

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
  it("shows the headline score + six pillar columns", () => {
    render(
      <HoldingsTable
        baseCurrency="USD"
        priced
        holdings={[
          h("AAPL", {
            attractiveness: 72.4,
            attractiveness_coverage: 6,
            attractiveness_quality: 90,
            attractiveness_value: 30,
            attractiveness_momentum: 85,
            attractiveness_growth: 80,
            attractiveness_stewardship: 70,
            attractiveness_defensiveness: 60,
          }),
        ]}
        visibleBands={["Attractiveness"]}
      />,
    );
    expect(screen.getAllByText("Attractiveness").length).toBeGreaterThan(0);
    expect(screen.getByText("Score")).toBeInTheDocument();
    expect(screen.getByText("72.4")).toBeInTheDocument();
    expect(screen.getByText("90")).toBeInTheDocument();
    expect(screen.getByText("30")).toBeInTheDocument();
    expect(screen.getByText("85")).toBeInTheDocument();
  });

  it("renders — for a coverage gap, never a fabricated value", () => {
    render(
      <HoldingsTable
        baseCurrency="USD"
        priced
        holdings={[
          h("ZZZ", {
            attractiveness: null,
            attractiveness_coverage: null,
            attractiveness_quality: null,
            attractiveness_value: null,
            attractiveness_momentum: null,
            attractiveness_growth: null,
            attractiveness_stewardship: null,
            attractiveness_defensiveness: null,
          }),
        ]}
        visibleBands={["Attractiveness"]}
      />,
    );
    expect(screen.getAllByText("Attractiveness").length).toBeGreaterThan(0);
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
  });
});
