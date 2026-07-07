// Live/close provenance markers on the Holdings table (metron-ops#147): while the intraday
// overlay is applied (LiveValuationProvider live=true), the columns that revalue from the
// delayed live quotes (Market value / Last / Unrealized / Day) carry a live dot in their
// header and the close-anchored analytic bands carry a "· close" band marker. Without the
// provider (watchlist compare, cost-basis views) or with the overlay off, no live claims
// render anywhere.

import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn() }) }));
vi.mock("@/app/portfolios/[id]/actions", () => ({
  setSecurityLabelAction: vi.fn(),
  setSecurityClassificationAction: vi.fn(),
}));

import { HoldingsTable } from "@/components/holdings-table";
import { LiveValuationProvider } from "@/components/live-valuation-context";
import type { Holding } from "@/lib/api";

const h = (ticker: string, overrides: Partial<Holding> = {}): Holding =>
  ({
    ticker,
    quantity: 10,
    avg_cost: 100,
    cost_basis: 1000,
    currency: "USD",
    fx_rate: 1,
    last_price: 120,
    last_price_date: "2026-07-06",
    last_price_stale: false,
    is_estimated: false,
    market_value_local: 1200,
    cost_basis_base: 1000,
    market_value: 1200,
    unrealized_gain: 200,
    unrealized_pct: 0.2,
    day_pct: 0.01,
    security_type: "equity",
    account_id: null,
    account_label: null,
    sector: null,
    country: "United States",
    ...overrides,
  }) as Holding;

const BANDS = ["Value", "Returns", "Valuation"] as const;

describe("live/close provenance markers", () => {
  it("marks the live columns and close-anchored bands while the overlay is applied", () => {
    render(
      <LiveValuationProvider live>
        <HoldingsTable baseCurrency="USD" priced holdings={[h("AAPL")]} visibleBands={[...BANDS]} />
      </LiveValuationProvider>,
    );
    for (const label of ["Market value", "Last", "Unrealized $", "Unrealized %", "Day"]) {
      const dot = screen.getByLabelText(`${label} is live`);
      expect(dot).toBeInTheDocument();
      expect(dot.getAttribute("title")).toMatch(/delayed intraday/i);
    }
    // Close-anchored columns never get a dot — the boundary inside the mixed Returns band.
    expect(screen.queryByLabelText("YTD is live")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("LTM is live")).not.toBeInTheDocument();
    // The analytic band header declares its close anchoring.
    expect(screen.getByText("· close")).toBeInTheDocument();
  });

  it("makes no live claims when the overlay is off", () => {
    render(
      <LiveValuationProvider live={false}>
        <HoldingsTable baseCurrency="USD" priced holdings={[h("AAPL")]} visibleBands={[...BANDS]} />
      </LiveValuationProvider>,
    );
    expect(screen.queryByLabelText(/is live$/)).not.toBeInTheDocument();
    expect(screen.queryByText("· close")).not.toBeInTheDocument();
  });

  it("defaults to no live claims without a provider (watchlist / cost-basis surfaces)", () => {
    render(<HoldingsTable baseCurrency="USD" priced holdings={[h("AAPL")]} visibleBands={[...BANDS]} />);
    expect(screen.queryByLabelText(/is live$/)).not.toBeInTheDocument();
    expect(screen.queryByText("· close")).not.toBeInTheDocument();
  });
});
