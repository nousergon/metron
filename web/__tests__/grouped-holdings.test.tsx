// GroupedHoldings — partitions holdings by security type into labelled groups (each a
// HoldingsTable with its own subtotal) + a portfolio grand-total bar; a single type
// renders as the bare table (no headings), preserving the prior look.

import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

// GroupedHoldings → HoldingsTable (a client component) → next/navigation + the server
// actions module (the inline alias editor). Stub both so the import chain works in jsdom.
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn() }) }));
vi.mock("@/app/portfolios/[id]/actions", () => ({ setSecurityLabelAction: vi.fn() }));

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
    // The asset-class group HEADING (not the Balance-Sheet "Cash" column header).
    expect(screen.getByRole("heading", { level: 3, name: /Cash/ })).toBeInTheDocument();
    expect(screen.getByText("Bonds")).toBeInTheDocument();
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

  it("orders groups canonically: equities, bonds, then cash", () => {
    const holdings = [h("AAPL", "equity"), h("VMFXX", "cash"), h("037833100", "bond")];
    render(<GroupedHoldings holdings={holdings} baseCurrency="USD" priced />);
    const headings = screen.getAllByRole("heading", { level: 3 }).map((n) => n.textContent);
    const order = headings.map((t) => t!.replace(/\d.*/, "").trim());
    expect(order).toEqual(["Equities", "Bonds", "Cash"]);
  });

  it("appends an unrecognized security_type under its raw key (never drops a holding)", () => {
    const holdings = [h("AAPL", "equity"), h("BTC", "crypto")];
    render(<GroupedHoldings holdings={holdings} baseCurrency="USD" priced />);
    // The unrecognized type heads its own section under its raw key (the Type column also
    // renders "crypto" now, so scope to the section heading).
    expect(screen.getByRole("heading", { level: 3, name: /crypto/ })).toBeInTheDocument();
    expect(screen.getByText("BTC")).toBeInTheDocument();
  });

  it("shows a plain 'prices as of' caption (latest close date) when the feed is fresh", () => {
    const holdings = [h("AAPL", "equity", { last_price_date: "2026-06-23" }), h("MSFT", "equity", { last_price_date: "2026-06-24" })];
    render(<GroupedHoldings holdings={holdings} baseCurrency="USD" priced />);
    // Latest close date wins; no stale warning.
    expect(screen.getByText(/Prices as of Jun 24, 2026\./)).toBeInTheDocument();
    expect(screen.queryByText(/feed hasn’t updated/)).not.toBeInTheDocument();
  });

  it("escalates to a stale warning when any holding is flagged stale", () => {
    const holdings = [h("AAPL", "equity", { last_price_date: "2026-06-23", last_price_stale: true })];
    render(<GroupedHoldings holdings={holdings} baseCurrency="USD" priced />);
    expect(screen.getByText(/Prices as of Jun 23, 2026/)).toBeInTheDocument();
    expect(screen.getByText(/feed hasn’t updated since/)).toBeInTheDocument();
  });

  it("omits the caption on the price-free (cost-basis-only) view", () => {
    const holdings = [h("AAPL", "equity")];
    render(<GroupedHoldings holdings={holdings} baseCurrency="USD" priced={false} />);
    expect(screen.queryByText(/Prices as of/)).not.toBeInTheDocument();
  });

  it("shows a plain 'positions synced through' caption for the oldest broker-sourced holding", () => {
    const holdings = [
      h("AAPL", "equity", { broker_as_of: "2026-06-24" }),
      h("PLTR", "equity", { broker_as_of: "2026-06-20" }),
    ];
    render(<GroupedHoldings holdings={holdings} baseCurrency="USD" priced />);
    // The OLDEST contributing account wins (the worst-case freshness), not the newest.
    expect(screen.getByText(/Positions synced through Jun 20, 2026\./)).toBeInTheDocument();
  });

  it("escalates to a stale positions warning when any holding's broker sync is stale", () => {
    const holdings = [h("PLTR", "equity", { broker_as_of: "2026-06-20", positions_stale: true })];
    render(<GroupedHoldings holdings={holdings} baseCurrency="USD" priced />);
    expect(screen.getByText(/Positions synced through Jun 20, 2026/)).toBeInTheDocument();
    expect(screen.getByText(/may not be reflected yet/)).toBeInTheDocument();
  });

  it("omits the positions caption for ledger-only (CSV\\/OFX) holdings", () => {
    const holdings = [h("AAPL", "equity")]; // no broker_as_of — ledger-derived
    render(<GroupedHoldings holdings={holdings} baseCurrency="USD" priced />);
    expect(screen.queryByText(/Positions synced through/)).not.toBeInTheDocument();
  });

  it("collapses and re-expands a group's table on heading click, defaulting open (metron-ops#178)", async () => {
    const user = userEvent.setup();
    const holdings = [h("AAPL", "equity"), h("037833100", "bond")];
    render(<GroupedHoldings holdings={holdings} baseCurrency="USD" priced />);
    // Defaults open — every row visible without interaction.
    expect(screen.getByText("AAPL")).toBeInTheDocument();
    expect(screen.getByText("037833100")).toBeInTheDocument();

    await user.click(screen.getByRole("heading", { level: 3, name: /Equities/ }));
    expect(screen.queryByText("AAPL")).not.toBeInTheDocument();
    // The other group is unaffected — collapse is per-section, not page-wide.
    expect(screen.getByText("037833100")).toBeInTheDocument();

    await user.click(screen.getByRole("heading", { level: 3, name: /Equities/ }));
    expect(screen.getByText("AAPL")).toBeInTheDocument();
  });
});
