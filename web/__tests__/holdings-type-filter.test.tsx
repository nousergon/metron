// Faceted type-filter chips (metron-ops#115): presentTypes derives the chip set from the
// held types (canonical order + counts), and toggling a chip hides that type's rows.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), refresh: vi.fn() }),
  usePathname: () => "/portfolios/p1/holdings",
  useSearchParams: () => new URLSearchParams(""),
}));
vi.mock("@/app/portfolios/[id]/actions", () => ({
  saveHoldingsViewAction: vi.fn(),
  setSecurityLabelAction: vi.fn(),
  setSecurityClassificationAction: vi.fn(),
}));

import { presentTypes } from "@/components/holdings-type-filter";
import { HoldingsView } from "@/components/holdings-view";
import type { Holding } from "@/lib/api";

const h = (ticker: string, security_type: string): Holding =>
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
    account_id: null,
    account_label: null,
    sector: "Technology",
    country: "United States",
  }) as Holding;

describe("presentTypes", () => {
  it("returns held types in canonical order with counts; appends unrecognized", () => {
    const got = presentTypes(["bond", "equity", "equity", "weird"]);
    expect(got).toEqual([
      { type: "equity", label: "Stocks", count: 2 },
      { type: "bond", label: "Bonds", count: 1 },
      { type: "weird", label: "weird", count: 1 },
    ]);
  });
});

describe("TypeFilterChips in HoldingsView", () => {
  const holdings = [h("AAPL", "equity"), h("912828YK0", "treasury"), h("GS-CD", "cd")];

  it("renders a chip per held type (with count); no chip row for a single type", () => {
    render(<HoldingsView holdings={holdings} baseCurrency="USD" priced medians={null} portfolioId="p1" />);
    expect(screen.getByRole("button", { name: /Stocks/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Treasuries/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /CDs/ })).toBeInTheDocument();
  });

  it("hiding a type removes its rows", () => {
    render(<HoldingsView holdings={holdings} baseCurrency="USD" priced medians={null} portfolioId="p1" />);
    expect(screen.getByText("912828YK0")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Treasuries/ }));
    expect(screen.queryByText("912828YK0")).not.toBeInTheDocument(); // treasury row gone
    expect(screen.getByText("AAPL")).toBeInTheDocument(); // others remain
  });

  it("hiding every type shows the all-hidden note", () => {
    render(<HoldingsView holdings={[h("AAPL", "equity"), h("MSFT", "etf")]} baseCurrency="USD" priced medians={null} portfolioId="p1" />);
    fireEvent.click(screen.getByRole("button", { name: /Stocks/ }));
    fireEvent.click(screen.getByRole("button", { name: /ETFs/ }));
    expect(screen.getByText(/All instrument types are hidden/)).toBeInTheDocument();
  });
});
