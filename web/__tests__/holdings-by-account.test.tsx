// Uncombined per-account view (metron-ops#114): HoldingsTable renders an Account column
// (carrying each row's account_label) when `accountColumn` is set, and not otherwise.

import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn() }) }));
vi.mock("@/app/portfolios/[id]/actions", () => ({
  setSecurityLabelAction: vi.fn(),
  setSecurityClassificationAction: vi.fn(),
}));

import { HoldingsTable } from "@/components/holdings-table";
import type { Holding } from "@/lib/api";

const h = (ticker: string, account_label: string | null, over: Partial<Holding> = {}): Holding =>
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
    account_id: account_label ? "acct-id" : null,
    account_label,
    sector: "Technology",
    country: "United States",
    ...over,
  }) as Holding;

describe("HoldingsTable Account column", () => {
  it("renders the Account header + per-row account labels when accountColumn is set", () => {
    render(
      <HoldingsTable
        baseCurrency="USD"
        priced
        accountColumn
        holdings={[h("AAPL", "Brokerage"), h("AAPL", "IRA")]}
      />,
    );
    expect(screen.getByRole("button", { name: /Account/ })).toBeInTheDocument(); // sortable header
    expect(screen.getByText("Brokerage")).toBeInTheDocument();
    expect(screen.getByText("IRA")).toBeInTheDocument();
  });

  it("omits the Account column on the consolidated (default) view", () => {
    render(<HoldingsTable baseCurrency="USD" priced holdings={[h("AAPL", null)]} />);
    expect(screen.queryByRole("button", { name: /^Account$/ })).not.toBeInTheDocument();
  });
});
