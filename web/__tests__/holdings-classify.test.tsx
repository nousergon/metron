// Inline sector / country classification override in HoldingsTable — an UNCLASSIFIED
// holding shows a "Set …" dropdown to fill the gap; a classified one shows the value with
// a ✎ to correct it (choosing the blank option clears the override). Read-only contexts
// (no portfolioId) expose no editor. Tenant-scoped override (never mutates the shared row).

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

const mocks = vi.hoisted(() => ({
  refresh: vi.fn(),
  setSecurityLabelAction: vi.fn(async () => ({ ok: true, message: "" })),
  setSecurityClassificationAction: vi.fn(
    async (_pid: string, _sym: string, _field: string, _value: string) => ({ ok: true, message: "" }),
  ),
}));
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: mocks.refresh }) }));
vi.mock("@/app/portfolios/[id]/actions", () => ({
  setSecurityLabelAction: mocks.setSecurityLabelAction,
  setSecurityClassificationAction: mocks.setSecurityClassificationAction,
}));

import { HoldingsTable } from "@/components/holdings-table";
import type { Holding } from "@/lib/api";

const h = (ticker: string, sector: string | null, country: string | null): Holding =>
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
    security_type: "equity",
    user_label: null,
    sector,
    country,
  }) as Holding;

beforeEach(() => {
  mocks.refresh.mockClear();
  mocks.setSecurityClassificationAction.mockClear();
});

describe("HoldingsTable sector/country override", () => {
  it("shows a 'Set' dropdown for an unclassified holding and saves the choice", async () => {
    render(<HoldingsTable holdings={[h("912828YK0", null, null)]} baseCurrency="USD" priced={false} portfolioId="p" />);
    const countrySelect = screen.getByLabelText("Set country for 912828YK0");
    fireEvent.change(countrySelect, { target: { value: "United States" } });
    await waitFor(() =>
      expect(mocks.setSecurityClassificationAction).toHaveBeenCalledWith("p", "912828YK0", "country", "United States"),
    );
    await waitFor(() => expect(mocks.refresh).toHaveBeenCalled());
  });

  it("a classified holding shows the value + ✎; editing swaps to a dropdown (correct/clear)", async () => {
    render(<HoldingsTable holdings={[h("AXON", "Industrials", "United States")]} baseCurrency="USD" priced={false} portfolioId="p" />);
    // No dropdown until the ✎ is clicked — the value renders plainly.
    expect(screen.getByText("Industrials")).toBeInTheDocument();
    expect(screen.queryByLabelText("Set sector for AXON")).not.toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("Edit sector for AXON"));
    fireEvent.change(screen.getByLabelText("Set sector for AXON"), { target: { value: "Technology" } });
    await waitFor(() =>
      expect(mocks.setSecurityClassificationAction).toHaveBeenCalledWith("p", "AXON", "sector", "Technology"),
    );
  });

  it("exposes no editor in a read-only context (no portfolioId)", () => {
    render(<HoldingsTable holdings={[h("AAPL", null, null)]} baseCurrency="USD" priced={false} />);
    expect(screen.queryByLabelText("Set country for AAPL")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Set sector for AAPL")).not.toBeInTheDocument();
  });
});
