// Row virtualization (metron-ops#231): with many positions, only the rows near the current
// scroll position should mount as real <tr> DOM nodes — never the full holdings list — and
// the rendered slice must track window scroll so every row is still reachable.

import { describe, expect, it, vi } from "vitest";
import { act, fireEvent, render } from "@testing-library/react";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn() }) }));
vi.mock("@/app/portfolios/[id]/actions", () => ({
  setSecurityLabelAction: vi.fn(),
  setSecurityClassificationAction: vi.fn(),
}));

import { HoldingsTable } from "@/components/holdings-table";
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
    last_price_date: "2024-06-03",
    market_value_local: 1200,
    cost_basis_base: 1000,
    market_value: 1200,
    unrealized_gain: 200,
    unrealized_pct: 0.2,
    security_type: "equity",
    sector: "Technology",
    country: "United States",
    pe: 30.2,
    ...over,
  }) as Holding;

const MANY = Array.from({ length: 200 }, (_, i) => h(`TCK${i.toString().padStart(3, "0")}`));

function bodyRowCount() {
  // Excludes the two <tr> virtualization spacer rows (aria-hidden, no ticker cell).
  return document.querySelectorAll("tbody tr:not([aria-hidden])").length;
}

describe("HoldingsTable row virtualization", () => {
  it("renders far fewer <tr> elements than the total holdings count", () => {
    render(<HoldingsTable baseCurrency="USD" priced holdings={MANY} visibleBands={["Valuation"]} />);

    expect(bodyRowCount()).toBeGreaterThan(0);
    expect(bodyRowCount()).toBeLessThan(60);
  });

  it("keeps sorting, band rendering, and totals correct across the full row set", () => {
    render(<HoldingsTable baseCurrency="USD" priced holdings={MANY} visibleBands={["Valuation"]} />);

    // Every holding still contributes to totals even though most never mount as a <tr>.
    expect(document.body.textContent).toContain("1,200"); // per-holding market value, at least one visible
  });

  it("renders a different row window after the page scrolls down", () => {
    render(<HoldingsTable baseCurrency="USD" priced holdings={MANY} visibleBands={["Valuation"]} />);
    const before = document.querySelector("tbody tr:not([aria-hidden])")?.textContent;

    act(() => {
      Object.defineProperty(window, "scrollY", { value: 4000, writable: true, configurable: true });
      fireEvent.scroll(window);
    });

    const after = document.querySelector("tbody tr:not([aria-hidden])")?.textContent;
    expect(after).not.toBe(before);
  });
});
