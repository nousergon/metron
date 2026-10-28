// Tearsheet "By account" breakdown — which accounts hold the security, with each
// account's own quantity, value and return.

import { describe, expect, it } from "vitest";
import { render, screen, within } from "@testing-library/react";

import { TearsheetAccounts } from "@/components/tearsheet-accounts";
import type { TearsheetAccountPosition } from "@/lib/api";

const leg = (over: Partial<TearsheetAccountPosition> = {}): TearsheetAccountPosition => ({
  account_id: "a1",
  account_label: "Brokerage",
  account_type: null,
  quantity: 30,
  avg_cost: 70,
  cost_basis: 2100,
  market_value: 1800,
  unrealized_gain: -300,
  unrealized_pct: -0.142857,
  share_pct: 0.75,
  ...over,
});

describe("TearsheetAccounts", () => {
  it("lists one row per holding account with quantity, value and return", () => {
    render(
      <TearsheetAccounts
        legs={[
          leg(),
          leg({ account_id: "a2", account_label: "Roth IRA", account_type: "ira", quantity: 10, avg_cost: 55, cost_basis: 550, market_value: 600, unrealized_gain: 50, unrealized_pct: 0.0909, share_pct: 0.25 }),
        ]}
        baseCurrency="USD"
        nativeCurrency="USD"
      />,
    );
    expect(screen.getByText("By account")).toBeInTheDocument();
    expect(screen.getByText("2 accounts · values in USD")).toBeInTheDocument();
    const rows = screen.getAllByRole("row").slice(1);
    expect(rows).toHaveLength(2);
    expect(within(rows[0]).getByText("Brokerage")).toBeInTheDocument();
    expect(within(rows[0]).getByText("30")).toBeInTheDocument();
    expect(within(rows[1]).getByText("Roth IRA")).toBeInTheDocument();
    expect(within(rows[1]).getByText("ira")).toBeInTheDocument();
  });

  it("shows a dash for an unpriced account rather than a zero", () => {
    render(
      <TearsheetAccounts
        legs={[leg({ market_value: null, unrealized_gain: null, unrealized_pct: null, share_pct: null })]}
        baseCurrency="USD"
        nativeCurrency="USD"
      />,
    );
    const row = screen.getAllByRole("row")[1];
    expect(within(row).getAllByText("—")).toHaveLength(4);
  });

  it("renders nothing when no account legs are present", () => {
    const { container } = render(<TearsheetAccounts legs={[]} baseCurrency="USD" nativeCurrency="USD" />);
    expect(container).toBeEmptyDOMElement();
  });
});
