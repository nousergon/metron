// What-if reallocation sandbox panel (metron-ops#171) — the collapsible Holdings-page
// panel. Covers rendering, weight editing (zero / normalize / reset), and that the
// panel never mutates the passed-in holdings prop (ephemeral, client-state only).

import { describe, expect, it } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { HoldingsWhatIfPanel } from "@/components/holdings-whatif-panel";
import type { Holding } from "@/lib/api";

const h = (ticker: string, over: Partial<Holding> = {}): Holding =>
  ({
    ticker,
    quantity: 10,
    avg_cost: 100,
    cost_basis: 1000,
    currency: "USD",
    fx_rate: 1,
    last_price: 100,
    last_price_date: "2026-07-01",
    market_value_local: 1000,
    cost_basis_base: 1000,
    market_value: 1000,
    unrealized_gain: 0,
    unrealized_pct: 0,
    security_type: "equity",
    sector: "Technology",
    country: "United States",
    attractiveness: null,
    ...over,
  }) as Holding;

function openPanel() {
  // Native <details> renders content in the DOM regardless of the `open` attribute in
  // jsdom, but exercise the real disclosure toggle for realism.
  const summary = screen.getByText("What-if: try hypothetical weights");
  fireEvent.click(summary);
}

describe("HoldingsWhatIfPanel", () => {
  it("renders a collapsible panel with baseline weights beside editable hypothetical inputs", () => {
    const holdings = [h("AAPL", { market_value: 600 }), h("MSFT", { market_value: 400, sector: "Technology" })];
    render(<HoldingsWhatIfPanel holdings={holdings} />);
    openPanel();
    expect(screen.getByText("AAPL")).toBeInTheDocument();
    expect(screen.getByText("MSFT")).toBeInTheDocument();
    expect(screen.getByText("60.0%")).toBeInTheDocument(); // baseline weight column
    expect(screen.getByText("Cash (residual)")).toBeInTheDocument();
  });

  it("weights + cash tie out to 100% on initial render (seeded from baseline)", () => {
    const holdings = [h("AAPL", { market_value: 700 }), h("MSFT", { market_value: 300 })];
    render(<HoldingsWhatIfPanel holdings={holdings} />);
    openPanel();
    expect(screen.getByText(/Hypothetical weights \+ cash: 100\.0%/)).toBeInTheDocument();
  });

  it("zeroing a holding sends its weight to cash and does not touch the other ticker", () => {
    const holdings = [h("AAPL", { market_value: 500 }), h("MSFT", { market_value: 500 })];
    render(<HoldingsWhatIfPanel holdings={holdings} />);
    openPanel();
    const row = screen.getByText("AAPL").closest("tr")!;
    fireEvent.click(within(row).getByText("Zero"));
    const inputs = screen.getAllByLabelText("Hypothetical weight (%)");
    expect((inputs[0] as HTMLInputElement).value).toBe("0"); // AAPL row
    expect((inputs[1] as HTMLInputElement).value).toBe("50"); // MSFT untouched
    expect(screen.getByText(/Hypothetical weights \+ cash: 100\.0%/)).toBeInTheDocument();
  });

  it("editing a weight past 100% flags the tie-out line until Normalize is used", () => {
    const holdings = [h("AAPL", { market_value: 500 }), h("MSFT", { market_value: 500 })];
    render(<HoldingsWhatIfPanel holdings={holdings} />);
    openPanel();
    const inputs = screen.getAllByLabelText("Hypothetical weight (%)");
    fireEvent.change(inputs[0], { target: { value: "90" } });
    expect(screen.getByText(/does not tie to 100%/)).toBeInTheDocument();

    fireEvent.click(screen.getByText("Normalize to 100%"));
    expect(screen.getByText(/Hypothetical weights \+ cash: 100\.0%/)).toBeInTheDocument();
    expect(screen.queryByText(/does not tie to 100%/)).not.toBeInTheDocument();
  });

  it("Zero all clears every ticker's hypothetical weight to cash in one click", () => {
    const holdings = [h("AAPL", { market_value: 500 }), h("MSFT", { market_value: 500 })];
    render(<HoldingsWhatIfPanel holdings={holdings} />);
    openPanel();
    fireEvent.click(screen.getByText("Zero all"));
    const inputs = screen.getAllByLabelText("Hypothetical weight (%)");
    expect((inputs[0] as HTMLInputElement).value).toBe("0");
    expect((inputs[1] as HTMLInputElement).value).toBe("0");
    expect(screen.getByText(/Hypothetical weights \+ cash: 100\.0%/)).toBeInTheDocument();
  });

  it("Reset to current restores every hypothetical weight to the baseline", () => {
    const holdings = [h("AAPL", { market_value: 500 }), h("MSFT", { market_value: 500 })];
    render(<HoldingsWhatIfPanel holdings={holdings} />);
    openPanel();
    const inputs = screen.getAllByLabelText("Hypothetical weight (%)");
    fireEvent.change(inputs[0], { target: { value: "10" } });
    fireEvent.click(screen.getByText("Reset to current"));
    const resetInputs = screen.getAllByLabelText("Hypothetical weight (%)");
    expect((resetInputs[0] as HTMLInputElement).value).toBe("50");
  });

  it("never mutates the holdings prop it was given (ephemeral client state only)", () => {
    const holdings = [h("AAPL", { market_value: 500 }), h("MSFT", { market_value: 500 })];
    const snapshot = JSON.parse(JSON.stringify(holdings));
    render(<HoldingsWhatIfPanel holdings={holdings} />);
    openPanel();
    const inputs = screen.getAllByLabelText("Hypothetical weight (%)");
    fireEvent.change(inputs[0], { target: { value: "0" } });
    fireEvent.click(screen.getByText("Normalize to 100%"));
    expect(JSON.parse(JSON.stringify(holdings))).toEqual(snapshot);
  });

  it("shows the weighted-Attractiveness N/A row as em-dash for an all-uncovered portfolio", () => {
    const holdings = [h("ZZZZ", { market_value: 1000, attractiveness: null })];
    render(<HoldingsWhatIfPanel holdings={holdings} />);
    openPanel();
    expect(screen.getByText("Portfolio score (0–100)")).toBeInTheDocument();
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
  });
});
