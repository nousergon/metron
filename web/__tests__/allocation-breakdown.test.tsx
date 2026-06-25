// AllocationBreakdown — US-vs-international split by country of domicile + a by-sector
// weighting, over priced non-cash holdings; cash excluded, unclassified surfaced honestly.

import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import { AllocationBreakdown } from "@/components/allocation-breakdown";
import type { Holding } from "@/lib/api";

const h = (ticker: string, over: Partial<Holding> = {}): Holding =>
  ({
    ticker,
    quantity: 10,
    avg_cost: 100,
    cost_basis: 1000,
    currency: "USD",
    fx_rate: 1,
    last_price: 120,
    last_price_date: "2026-06-24",
    last_price_stale: false,
    market_value_local: 1000,
    cost_basis_base: 1000,
    market_value: 1000,
    unrealized_gain: 0,
    unrealized_pct: 0,
    security_type: "equity",
    user_label: null,
    overnight_pct: null,
    intraday_pct: null,
    day_pct: null,
    ytd_pct: null,
    ltm_pct: null,
    sector: "Technology",
    country: "United States",
    ...over,
  }) as Holding;

describe("AllocationBreakdown", () => {
  it("splits US vs International by domicile value (excluding cash)", () => {
    const holdings = [
      h("AAPL", { country: "United States", market_value: 3000 }),
      h("ASML", { country: "Netherlands", market_value: 1000 }),
      h("VMFXX", { security_type: "cash", country: "United States", market_value: 5000 }), // excluded
    ];
    render(<AllocationBreakdown holdings={holdings} baseCurrency="USD" />);
    // 3000 US / 4000 classified = 75%; 1000 Intl = 25%.
    expect(screen.getByText("75.0%")).toBeInTheDocument();
    expect(screen.getByText("25.0%")).toBeInTheDocument();
  });

  it("surfaces unclassified-country value rather than guessing a bucket", () => {
    const holdings = [
      h("AAPL", { country: "United States", market_value: 1000 }),
      h("MYSTERY", { country: null, market_value: 1000 }),
    ];
    render(<AllocationBreakdown holdings={holdings} baseCurrency="USD" />);
    expect(screen.getByText(/no resolved country/)).toBeInTheDocument();
  });

  it("notes that fund domicile is not underlying exposure", () => {
    render(<AllocationBreakdown holdings={[h("AAPL")]} baseCurrency="USD" />);
    expect(screen.getByText(/not its underlying geographic exposure/)).toBeInTheDocument();
  });
});
