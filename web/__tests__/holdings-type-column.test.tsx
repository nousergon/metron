// Editable Type column (metron-ops#115): the Holdings table shows each holding's
// instrument type via a friendly label and, with a portfolioId, lets the user override it.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

const setSecurityClassificationAction = vi.fn((..._args: unknown[]) =>
  Promise.resolve({ ok: true, message: "Type saved." }),
);
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn() }) }));
vi.mock("@/app/portfolios/[id]/actions", () => ({
  setSecurityLabelAction: vi.fn(),
  setSecurityClassificationAction: (...args: unknown[]) => setSecurityClassificationAction(...args),
}));

import { HoldingsTable } from "@/components/holdings-table";
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

describe("HoldingsTable Type column", () => {
  it("renders the friendly type label for each holding", () => {
    render(<HoldingsTable baseCurrency="USD" priced holdings={[h("912828YK0", "treasury")]} />);
    expect(screen.getByRole("button", { name: /Type/ })).toBeInTheDocument(); // sortable header
    expect(screen.getByText("Treasury")).toBeInTheDocument();
  });

  it("overrides the type via the dropdown (sends the key, not the label)", () => {
    render(<HoldingsTable baseCurrency="USD" priced portfolioId="p1" holdings={[h("912828YK0", "bond")]} />);
    fireEvent.click(screen.getByRole("button", { name: /Edit type for 912828YK0/ }));
    fireEvent.change(screen.getByRole("combobox", { name: /Set type for 912828YK0/ }), {
      target: { value: "treasury" },
    });
    expect(setSecurityClassificationAction).toHaveBeenCalledWith("p1", "912828YK0", "type", "treasury");
  });
});
