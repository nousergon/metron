// Column-definition ⓘ signposts (metron-ops#115): an ambiguously-named column carries a
// plain-language definition surfaced as an ⓘ in its header (the title attr holds the text).

import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn() }) }));
vi.mock("@/app/portfolios/[id]/actions", () => ({
  setSecurityLabelAction: vi.fn(),
  setSecurityClassificationAction: vi.fn(),
}));

import { HoldingsTable } from "@/components/holdings-table";
import type { Holding } from "@/lib/api";

const h = (ticker: string): Holding =>
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
    account_id: null,
    account_label: null,
    sector: "Technology",
    country: "United States",
    attractiveness: 72,
  }) as Holding;

describe("column definitions", () => {
  it("shows an ⓘ signpost on a column that carries a definition (Unrealized %)", () => {
    render(<HoldingsTable baseCurrency="USD" priced holdings={[h("AAPL")]} />);
    // The Unrealized % header button carries the definition in its title.
    const header = screen.getByRole("button", { name: /Unrealized %/ });
    expect(header.getAttribute("title")).toMatch(/cost basis/i);
    // A visible ⓘ glyph sits in the header.
    expect(header.textContent).toContain("ⓘ");
  });

  it("self-evident columns carry no definition (no ⓘ in the Ticker header)", () => {
    render(<HoldingsTable baseCurrency="USD" priced holdings={[h("AAPL")]} />);
    const ticker = screen.getByRole("button", { name: /Ticker/ });
    expect(ticker.textContent).not.toContain("ⓘ");
    expect(ticker).toHaveAttribute("title", "Sort by Ticker");
  });

  it("the Score column is defined (attractiveness composite)", () => {
    render(<HoldingsTable baseCurrency="USD" priced holdings={[h("AAPL")]} />);
    const score = screen.getByRole("button", { name: /Score/ });
    expect(score.getAttribute("title")).toMatch(/attractiveness/i);
  });
});
