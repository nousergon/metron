// TopBottomPerformers — ranks holdings best/worst by return over a period, by either the
// holding's own price return or its contribution (weight × return), with disjoint
// top/bottom lists and instant period/basis toggles.

import { describe, expect, it } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";

import { TopBottomPerformers } from "@/components/top-bottom-performers";
import type { Holding } from "@/lib/api";

const h = (ticker: string, over: Partial<Holding> = {}): Holding =>
  ({
    ticker,
    quantity: 10,
    avg_cost: 100,
    cost_basis: 1000,
    currency: "USD",
    fx_rate: 1,
    last_price: 120,
    last_price_date: "2026-06-24",
    last_price_stale: false,
    market_value_local: 1200,
    cost_basis_base: 1000,
    market_value: 1200,
    unrealized_gain: 200,
    unrealized_pct: 0.2,
    security_type: "equity",
    user_label: null,
    overnight_pct: null,
    intraday_pct: null,
    day_pct: null,
    ytd_pct: null,
    ltm_pct: null,
    sector: "Technology",
    country: "United States",
    ...over,
  }) as Holding;

function panel(title: string) {
  // The title sits in a header div inside the card; the card is its parent.
  return screen.getByText(title).parentElement as HTMLElement;
}

describe("TopBottomPerformers", () => {
  it("ranks by the holding's own YTD price return (best on top, worst on bottom)", () => {
    const holdings = [
      h("WIN", { ytd_pct: 0.5 }),
      h("MID", { ytd_pct: 0.1 }),
      h("LOSE", { ytd_pct: -0.3 }),
    ];
    render(<TopBottomPerformers holdings={holdings} />);
    // Default period = YTD, basis = price return.
    const top = within(panel("Top performers"));
    const bottom = within(panel("Bottom performers"));
    expect(top.getByText("WIN")).toBeInTheDocument();
    expect(bottom.getByText("LOSE")).toBeInTheDocument();
    expect(top.getByText("50.0%")).toBeInTheDocument();
    expect(bottom.getByText("(30.0%)")).toBeInTheDocument(); // accounting-style negative
  });

  it("excludes holdings with no return for the period and notes a missing Day feed", () => {
    const holdings = [h("AAPL", { ytd_pct: 0.2, day_pct: null })];
    render(<TopBottomPerformers holdings={holdings} />);
    fireEvent.click(screen.getByRole("button", { name: "Day" }));
    expect(screen.getByText(/intraday feed isn’t available/)).toBeInTheDocument();
  });

  it("contribution basis weights the return by position size", () => {
    // BIG is a 90% position up 10% (contrib +9%); SMALL is 10% up 50% (contrib +5%).
    const holdings = [
      h("BIG", { ytd_pct: 0.1, market_value: 9000 }),
      h("SMALL", { ytd_pct: 0.5, market_value: 1000 }),
    ];
    render(<TopBottomPerformers holdings={holdings} />);
    fireEvent.click(screen.getByRole("button", { name: "Contribution" }));
    // By price return SMALL (50%) would top BIG; by contribution BIG (+9%) tops.
    const top = within(panel("Top performers"));
    expect(top.getByText("BIG")).toBeInTheDocument();
    expect(top.getByText("9.0%")).toBeInTheDocument(); // contribution value
    expect(top.getByText("(10.0%)")).toBeInTheDocument(); // own return in parens
  });
});
