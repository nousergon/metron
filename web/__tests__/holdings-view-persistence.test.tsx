// Saved Holdings-table view (metron-ops#114): HoldingsView hydrates the grouping + visible
// bands from the saved values and persists every control change fire-and-forget.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

const push = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, refresh: vi.fn() }),
  usePathname: () => "/portfolios/p1/holdings",
  useSearchParams: () => new URLSearchParams(""),
}));
const saveHoldingsViewAction = vi.fn();
vi.mock("@/app/portfolios/[id]/actions", () => ({
  saveHoldingsViewAction: (...args: unknown[]) => saveHoldingsViewAction(...args),
  setSecurityLabelAction: vi.fn(),
  setSecurityClassificationAction: vi.fn(),
}));

import { HoldingsView } from "@/components/holdings-view";
import type { Holding } from "@/lib/api";

const h = (ticker: string): Holding =>
  ({
    ticker,
    quantity: 10,
    avg_cost: 100,
    cost_basis: 1000,
    currency: "USD",
    fx_rate: 1,
    last_price: 120,
    last_price_date: "2024-06-03",
    market_value_local: 1200,
    cost_basis_base: 1000,
    market_value: 1200,
    unrealized_gain: 200,
    unrealized_pct: 0.2,
    security_type: "equity",
    account_id: null,
    account_label: null,
    sector: "Technology",
    country: "United States",
    attractiveness: 70,
    pe: 30.2,
  }) as Holding;

describe("HoldingsView saved-view hydration", () => {
  it("hydrates the grouping + visible bands from the saved values", () => {
    render(
      <HoldingsView
        holdings={[h("AAPL")]}
        baseCurrency="USD"
        priced
        medians={null}
        portfolioId="p1"
        savedGrouping="classification"
        savedBands={["Attractiveness", "Valuation"]}
      />,
    );
    // Saved grouping is active...
    expect(screen.getByRole("button", { name: "By sector → country" })).toHaveAttribute("aria-pressed", "true");
    // ...and the saved Valuation band is shown — P/E (30.2×) is a Valuation-only cell.
    expect(screen.getByText("30.2×")).toBeInTheDocument();
  });

  it("defaults to asset-class + the lean Overview preset when nothing is saved", () => {
    render(<HoldingsView holdings={[h("AAPL")]} baseCurrency="USD" priced medians={null} portfolioId="p1" />);
    expect(screen.getByRole("button", { name: "By asset class" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.queryByText("30.2×")).not.toBeInTheDocument(); // Overview = Position + Value, no Valuation cells
  });

  it("anchors the column-band control beneath the Portfolio total bar (metron-ops#118+)", () => {
    render(<HoldingsView holdings={[h("AAPL")]} baseCurrency="USD" priced medians={null} portfolioId="p1" />);
    // The control ("Columns") renders with the total bar, not stranded in the top toolbar.
    expect(screen.getByText("Portfolio total")).toBeInTheDocument();
    expect(screen.getByText("Columns")).toBeInTheDocument();
  });

  it("persists the full view when a control changes", () => {
    render(<HoldingsView holdings={[h("AAPL")]} baseCurrency="USD" priced medians={null} portfolioId="p1" />);
    saveHoldingsViewAction.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "By sector → country" }));
    expect(saveHoldingsViewAction).toHaveBeenCalledWith("p1", {
      grouping: "classification",
      visible_bands: ["Position", "Value"],
      combine_by_account: false,
      hidden_types: [],
    });
  });
});
