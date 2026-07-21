// Column-definition ⓘ signposts (metron-ops#115): an ambiguously-named column carries a
// plain-language definition surfaced as a click-to-open ⓘ disclosure in its header, separate
// from the sort control (a nested-button design previously swallowed every click on the ⓘ
// into the column's sort toggle — metron#158 follow-up).

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
  it("shows a click-to-open ⓘ disclosure on a column that carries a definition (Unrealized %)", () => {
    render(<HoldingsTable baseCurrency="USD" priced holdings={[h("AAPL")]} />);
    // "Unrealized %" now renders twice — once in Value, once as the Intraday band's own
    // self-sufficient duplicate (metron-ops#178 dual-band-duplicate precedent) — so both
    // headers are checked, not just one.
    const headers = screen.getAllByRole("button", { name: /Unrealized %/ });
    expect(headers).toHaveLength(2);
    for (const header of headers) {
      // The sort button itself no longer carries the definition — just the sort affordance.
      expect(header).toHaveAttribute("title", "Sort by Unrealized %");
      expect(header).not.toHaveTextContent("ⓘ");
    }
    // A separate, independently-clickable ⓘ disclosure sits beside each sort button and
    // reveals the definition text — it is NOT nested inside the sort button (nested buttons
    // are invalid HTML and would swallow the click into the sort toggle).
    const infos = screen.getAllByLabelText("What is Unrealized %?");
    expect(infos).toHaveLength(2);
    for (const info of infos) {
      expect(info.tagName).toBe("SUMMARY");
      expect(headers.some((header) => header.contains(info))).toBe(false);
      expect(info.closest("button")).toBeNull();
      expect(info.closest("details")).toHaveTextContent(/cost basis/i);
    }
  });

  it("self-evident columns carry no definition (no ⓘ in the Ticker header)", () => {
    render(<HoldingsTable baseCurrency="USD" priced holdings={[h("AAPL")]} />);
    const ticker = screen.getByRole("button", { name: /Ticker/ });
    expect(ticker.textContent).not.toContain("ⓘ");
    expect(ticker).toHaveAttribute("title", "Sort by Ticker");
    expect(screen.queryByLabelText("What is Ticker?")).not.toBeInTheDocument();
  });

  it("the Score column is defined (attractiveness composite), reachable independent of sort", () => {
    render(<HoldingsTable baseCurrency="USD" priced holdings={[h("AAPL")]} />);
    const score = screen.getByRole("button", { name: /Score/ });
    expect(score).toHaveAttribute("title", "Sort by Score");
    const info = screen.getByLabelText("What is Score?");
    expect(score.contains(info)).toBe(false);
    expect(info.closest("details")).toHaveTextContent(/attractiveness/i);
  });
});
