// "Estimated" badge on the Holdings table Last column (metron-ops#112): a late-striking
// mutual fund (FNILX/FZILX/FTIHX) whose live price is a same-day tracking-proxy ETF
// estimate is flagged distinctly from the last_price_stale ⚠ warning — "estimated" is a
// normal, expected same-day state (reconciled to the true NAV tomorrow), not a problem.

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
    quantity: 10,
    avg_cost: 100,
    cost_basis: 1000,
    currency: "USD",
    fx_rate: 1,
    last_price: 120,
    last_price_date: "2026-06-30",
    last_price_stale: false,
    is_estimated: false,
    market_value_local: 1200,
    cost_basis_base: 1000,
    market_value: 1200,
    unrealized_gain: 200,
    unrealized_pct: 0.2,
    security_type: "fund",
    account_id: null,
    account_label: null,
    sector: null,
    country: "United States",
    ...overrides,
  }) as Holding;

describe("estimated badge", () => {
  it("renders the estimated marker + tooltip when is_estimated is true", () => {
    render(<HoldingsTable baseCurrency="USD" priced holdings={[h("FNILX", { is_estimated: true })]} />);
    // Price renders twice with every band visible (Value "Last" + the Valuation "Price"
    // duplicate, metron-ops#178 — same shared cell renderer) — both carry the marker.
    const markers = screen.getAllByLabelText("estimated");
    expect(markers.length).toBe(2);
    for (const marker of markers) {
      expect(marker.getAttribute("title")).toMatch(/tracking-proxy ETF/i);
      expect(marker.getAttribute("title")).toMatch(/reconcile/i);
    }
  });

  it("omits the estimated marker for a normally-quoted holding", () => {
    render(<HoldingsTable baseCurrency="USD" priced holdings={[h("AAPL", { is_estimated: false })]} />);
    expect(screen.queryByLabelText("estimated")).not.toBeInTheDocument();
  });

  it("does not reuse the stale ⚠ glyph for the estimated marker (distinct wording/icon)", () => {
    render(
      <HoldingsTable
        baseCurrency="USD"
        priced
        holdings={[h("FNILX", { is_estimated: true, last_price_stale: false })]}
      />
    );
    const markers = screen.getAllByLabelText("estimated");
    for (const marker of markers) {
      expect(marker.textContent).not.toContain("⚠");
    }
  });
});
