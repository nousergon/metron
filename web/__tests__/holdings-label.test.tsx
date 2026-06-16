// Inline ticker-alias editor in HoldingsTable (metron-ops#47) — a numeric-CUSIP holding
// can be named; the alias renders with the raw symbol beneath, and saving calls the
// action + refreshes. Read-only contexts (no portfolioId) expose no editor.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

const mocks = vi.hoisted(() => ({
  refresh: vi.fn(),
  setSecurityLabelAction: vi.fn(async (_pid: string, _sym: string, _label: string) => ({ ok: true, message: "" })),
}));
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: mocks.refresh }) }));
vi.mock("@/app/portfolios/[id]/actions", () => ({ setSecurityLabelAction: mocks.setSecurityLabelAction }));

import { HoldingsTable } from "@/components/holdings-table";
import type { Holding } from "@/lib/api";

const h = (ticker: string, user_label: string | null): Holding =>
  ({
    ticker,
    quantity: 10,
    avg_cost: 100,
    cost_basis: 1000,
    currency: "USD",
    fx_rate: 1,
    last_price: null,
    last_price_date: null,
    market_value_local: null,
    cost_basis_base: 1000,
    market_value: null,
    unrealized_gain: null,
    unrealized_pct: null,
    security_type: "bond",
    user_label,
  }) as Holding;

beforeEach(() => {
  mocks.refresh.mockClear();
  mocks.setSecurityLabelAction.mockClear();
});

describe("HoldingsTable ticker alias", () => {
  it("shows the alias with the raw symbol beneath when set", () => {
    render(<HoldingsTable holdings={[h("912828YK0", "US Treasury 2026")]} baseCurrency="USD" priced={false} portfolioId="p" />);
    expect(screen.getByText("US Treasury 2026")).toBeInTheDocument();
    expect(screen.getByText("912828YK0")).toBeInTheDocument();
  });

  it("edits and saves a label, then refreshes", async () => {
    render(<HoldingsTable holdings={[h("912828YK0", null)]} baseCurrency="USD" priced={false} portfolioId="p" />);
    fireEvent.click(screen.getByLabelText("Add label for 912828YK0"));
    fireEvent.change(screen.getByLabelText("Label for 912828YK0"), { target: { value: "T-Note 2026" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(mocks.setSecurityLabelAction).toHaveBeenCalledWith("p", "912828YK0", "T-Note 2026"));
    await waitFor(() => expect(mocks.refresh).toHaveBeenCalled());
  });

  it("exposes no editor in a read-only context (no portfolioId)", () => {
    render(<HoldingsTable holdings={[h("AAPL", null)]} baseCurrency="USD" priced={false} />);
    expect(screen.getByText("AAPL")).toBeInTheDocument();
    expect(screen.queryByLabelText("Add label for AAPL")).not.toBeInTheDocument();
  });
});
