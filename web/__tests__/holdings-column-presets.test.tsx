// Column presets (metron-ops#114, #118+, realigned #140): HoldingsTable renders only the
// visible bands over the always-on Ticker + Market Value spine, and ColumnPresetControl
// drives the visible-band set from presets + the Customize popover.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

// HoldingsTable (client) → next/navigation + the server-actions module. Stub both.
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn() }) }));
vi.mock("@/app/portfolios/[id]/actions", () => ({
  setSecurityLabelAction: vi.fn(),
  setSecurityClassificationAction: vi.fn(),
}));

import { HoldingsTable } from "@/components/holdings-table";
import { ColumnPresetControl, COLUMN_PRESETS, DEFAULT_VISIBLE_GROUPS } from "@/components/holdings-column-presets";
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
    last_price_date: "2024-06-03",
    market_value_local: 1200,
    cost_basis_base: 1000,
    market_value: 1200,
    unrealized_gain: 200,
    unrealized_pct: 0.2,
    security_type: "equity",
    sector: "Technology",
    country: "United States",
    attractiveness: 72,
    pe: 30.2,
    market_cap: 3.0e12,
    rsi_14: 61,
    ...over,
  }) as Holding;

describe("HoldingsTable visibleBands", () => {
  it("shows only the requested bands (Attractiveness-only hides Valuation/Technicals)", () => {
    render(<HoldingsTable baseCurrency="USD" priced holdings={[h("AAPL")]} visibleBands={["Attractiveness"]} />);
    expect(screen.getAllByText("Attractiveness").length).toBeGreaterThan(0); // band header
    expect(screen.getByText("Score")).toBeInTheDocument(); // headline column label
    expect(screen.queryByText("Valuation")).not.toBeInTheDocument();
    expect(screen.queryByText("Technicals")).not.toBeInTheDocument();
    expect(screen.queryByText("30.2×")).not.toBeInTheDocument(); // P/E (Valuation) hidden
  });

  it("shows a band when it's in the visible set", () => {
    render(
      <HoldingsTable baseCurrency="USD" priced holdings={[h("AAPL")]} visibleBands={["Attractiveness", "Valuation"]} />,
    );
    expect(screen.getByText("Valuation")).toBeInTheDocument();
    expect(screen.getByText("30.2×")).toBeInTheDocument(); // P/E now visible
    expect(screen.queryByText("Technicals")).not.toBeInTheDocument(); // still hidden
  });

  it("defaults to every band when the prop is omitted (backward-compatible)", () => {
    render(<HoldingsTable baseCurrency="USD" priced holdings={[h("AAPL")]} />);
    expect(screen.getByText("Valuation")).toBeInTheDocument();
    expect(screen.getByText("Technicals")).toBeInTheDocument();
  });

  it("holds Market Value constant in the frozen spine regardless of which band is visible", () => {
    render(
      <HoldingsTable baseCurrency="USD" priced holdings={[h("AAPL")]} visibleBands={["Attractiveness"]} showTotals={false} />,
    );
    expect(screen.getByText("Market value")).toBeInTheDocument();
    expect(screen.getByText("$1,200")).toBeInTheDocument(); // market_value cell, spine-rendered
  });

  it("drops the Market Value spine column when showMarketValue is false", () => {
    render(
      <HoldingsTable baseCurrency="USD" priced holdings={[h("AAPL")]} visibleBands={["Attractiveness"]} showMarketValue={false} />,
    );
    expect(screen.queryByText("Market value")).not.toBeInTheDocument();
  });
});

describe("ColumnPresetControl", () => {
  it("the default visible groups match the lean Overview preset (Position + Value)", () => {
    expect(DEFAULT_VISIBLE_GROUPS).toEqual(["Position", "Value"]);
    expect(COLUMN_PRESETS[0].key).toBe("overview");
  });

  it("clicking an analytic preset emits ONLY its own band — no Position/Value drag-along", () => {
    const onChange = vi.fn();
    render(<ColumnPresetControl value={["Position", "Value"]} onChange={onChange} />);
    fireEvent.click(screen.getByRole("button", { name: "Fundamentals" }));
    expect(onChange).toHaveBeenCalledWith(["Fundamentals"]);
  });

  it("has a dedicated Attractiveness preset (not bundled into another analytic preset)", () => {
    const onChange = vi.fn();
    render(<ColumnPresetControl value={["Position", "Value"]} onChange={onChange} />);
    fireEvent.click(screen.getByRole("button", { name: "Attractiveness" }));
    expect(onChange).toHaveBeenCalledWith(["Attractiveness"]);
  });

  it("toggling a Customize band adds it in canonical order", () => {
    const onChange = vi.fn();
    render(<ColumnPresetControl value={["Position", "Value"]} onChange={onChange} />);
    // jsdom doesn't toggle <details> on summary click — open it directly.
    const details = screen.getByText("Customize").closest("details");
    if (details) details.open = true;
    fireEvent.click(screen.getByRole("checkbox", { name: "Technicals" }));
    expect(onChange).toHaveBeenCalledWith(["Position", "Value", "Technicals"]);
  });

  it("marks the active preset via aria-pressed", () => {
    render(<ColumnPresetControl value={["Valuation"]} onChange={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Valuation" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Overview" })).toHaveAttribute("aria-pressed", "false");
  });
});
