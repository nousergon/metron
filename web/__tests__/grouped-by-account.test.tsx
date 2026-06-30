// Account grouping (metron-ops#114): GroupedByAccount sections the per-account rows into
// one HoldingsTable per account (heading = account, biggest by market value first), with a
// portfolio grand-total bar. A security in N accounts appears in N sections.

import { describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn() }) }));
vi.mock("@/app/portfolios/[id]/actions", () => ({
  setSecurityLabelAction: vi.fn(),
  setSecurityClassificationAction: vi.fn(),
}));

import { GroupedByAccount } from "@/components/grouped-by-account";
import type { Holding } from "@/lib/api";

const h = (ticker: string, account_label: string, market_value: number, over: Partial<Holding> = {}): Holding =>
  ({
    ticker,
    quantity: 10,
    avg_cost: 100,
    cost_basis: 1000,
    currency: "USD",
    fx_rate: 1,
    last_price: market_value / 10,
    last_price_date: "2024-06-03",
    market_value_local: market_value,
    cost_basis_base: 1000,
    market_value,
    unrealized_gain: market_value - 1000,
    unrealized_pct: (market_value - 1000) / 1000,
    security_type: "equity",
    account_id: "id-" + account_label,
    account_label,
    sector: "Technology",
    country: "United States",
    ...over,
  }) as Holding;

describe("GroupedByAccount", () => {
  it("renders one section per account; a multi-account ticker appears in each", () => {
    render(
      <GroupedByAccount
        baseCurrency="USD"
        priced
        holdings={[
          h("AAPL", "Brokerage", 5000),
          h("MSFT", "Brokerage", 3000),
          h("AAPL", "IRA", 2000),
        ]}
      />,
    );
    // Section headings name each account.
    expect(screen.getByRole("heading", { name: /Brokerage/ })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /IRA/ })).toBeInTheDocument();
    // AAPL appears in both sections → two ticker links.
    expect(screen.getAllByText("AAPL").length).toBe(2);
    // Portfolio grand-total bar present.
    expect(screen.getByText("Portfolio total")).toBeInTheDocument();
  });

  it("orders sections by market value desc (biggest account first)", () => {
    render(
      <GroupedByAccount
        baseCurrency="USD"
        priced
        holdings={[h("AAPL", "Small", 1000), h("MSFT", "Big", 9000)]}
      />,
    );
    const headings = screen.getAllByRole("heading", { level: 3 }).map((el) => el.textContent ?? "");
    expect(headings[0]).toMatch(/Big/);
    expect(headings[1]).toMatch(/Small/);
  });

  it("a single account renders the bare table (no grand-total bar)", () => {
    render(<GroupedByAccount baseCurrency="USD" priced holdings={[h("AAPL", "Solo", 1000)]} />);
    expect(screen.queryByText("Portfolio total")).not.toBeInTheDocument();
    expect(screen.getByText("AAPL")).toBeInTheDocument();
  });
});
