// Quantity-column "positions stale" marker (metron-ops#150): distinct from the existing
// Last-column price-staleness warning — this flags the SHARE COUNT as stale (the daily
// broker re-sync hasn't run recently), which last_price_stale cannot catch since a fresh
// close price can be multiplied by a stale, wrong share count and still look current.

import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn() }) }));
vi.mock("@/app/portfolios/[id]/actions", () => ({
  setSecurityLabelAction: vi.fn(),
  setSecurityClassificationAction: vi.fn(),
}));

import { HoldingsTable } from "@/components/holdings-table";
import type { Holding } from "@/lib/api";

const h = (ticker: string, overrides: Partial<Holding> = {}): Holding =>
  ({
    ticker,
    quantity: 100,
    avg_cost: 20,
    cost_basis: 2000,
    currency: "USD",
    fx_rate: 1,
    last_price: 25,
    last_price_date: "2026-07-07",
    last_price_stale: false,
    is_estimated: false,
    broker_as_of: null,
    positions_stale: false,
    market_value_local: 2500,
    cost_basis_base: 2000,
    market_value: 2500,
    unrealized_gain: 500,
    unrealized_pct: 0.25,
    security_type: "equity",
    account_id: null,
    account_label: null,
    sector: null,
    country: "United States",
    ...overrides,
  }) as Holding;

describe("positions-stale badge", () => {
  it("renders the ⚠ marker + tooltip on Quantity when positions_stale is true", () => {
    render(
      <HoldingsTable
        baseCurrency="USD"
        priced
        holdings={[h("PLTR", { positions_stale: true, broker_as_of: "2026-06-20" })]}
      />
    );
    const cell = screen.getByText("100").closest("span");
    expect(cell?.textContent).toContain("⚠");
    expect(cell?.getAttribute("title")).toMatch(/synced 2026-06-20/);
  });

  it("omits the marker for a freshly-synced broker position", () => {
    render(
      <HoldingsTable
        baseCurrency="USD"
        priced
        holdings={[h("PLTR", { positions_stale: false, broker_as_of: "2026-07-07" })]}
      />
    );
    const cell = screen.getByText("100").closest("span");
    expect(cell?.textContent).not.toContain("⚠");
  });

  it("omits the marker for ledger-only (CSV/OFX) holdings", () => {
    render(<HoldingsTable baseCurrency="USD" priced holdings={[h("AAPL")]} />);
    const cell = screen.getByText("100").closest("span");
    expect(cell?.textContent).not.toContain("⚠");
  });
});
