// GroupedHoldings — partitions holdings by security type into labelled groups (each a
// HoldingsTable with its own subtotal) + a portfolio grand-total bar; a single type
// renders as the bare table (no headings), preserving the prior look.

import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { GroupedHoldings } from "@/components/grouped-holdings";
import type { Holding } from "@/lib/api";

const h = (ticker: string, security_type: string, over: Partial<Holding> = {}): Holding =>
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
    security_type,
    ...over,
  }) as Holding;

describe("GroupedHoldings", () => {
  it("groups mixed types with headings + a portfolio total bar", () => {
    const holdings = [h("AAPL", "equity"), h("037833100", "bond"), h("VMFXX", "cash")];
    render(<GroupedHoldings holdings={holdings} baseCurrency="USD" priced />);
    expect(screen.getByText("Portfolio total")).toBeInTheDocument();
    expect(screen.getByText("Cash")).toBeInTheDocument();
    expect(screen.getByText("Bonds & CDs")).toBeInTheDocument();
    expect(screen.getByText("Equities")).toBeInTheDocument();
    // Each holding still renders inside its group's table.
    expect(screen.getByText("AAPL")).toBeInTheDocument();
    expect(screen.getByText("037833100")).toBeInTheDocument();
  });

  it("renders the bare table (no headings/total bar) for a single type", () => {
    const holdings = [h("AAPL", "equity"), h("MSFT", "equity")];
    render(<GroupedHoldings holdings={holdings} baseCurrency="USD" priced />);
    expect(screen.queryByText("Portfolio total")).not.toBeInTheDocument();
    expect(screen.queryByText("Equities")).not.toBeInTheDocument();
    expect(screen.getByText("AAPL")).toBeInTheDocument();
    expect(screen.getByText("MSFT")).toBeInTheDocument();
  });

  it("orders groups canonically: cash, bonds, then equities", () => {
    const holdings = [h("AAPL", "equity"), h("VMFXX", "cash"), h("037833100", "bond")];
    render(<GroupedHoldings holdings={holdings} baseCurrency="USD" priced />);
    const headings = screen.getAllByRole("heading", { level: 3 }).map((n) => n.textContent);
    const order = headings.map((t) => t!.replace(/\d.*/, "").trim());
    expect(order).toEqual(["Cash", "Bonds & CDs", "Equities"]);
  });

  it("appends an unrecognized security_type under its raw key (never drops a holding)", () => {
    const holdings = [h("AAPL", "equity"), h("BTC", "crypto")];
    render(<GroupedHoldings holdings={holdings} baseCurrency="USD" priced />);
    expect(screen.getByText("crypto")).toBeInTheDocument();
    expect(screen.getByText("BTC")).toBeInTheDocument();
  });
});
