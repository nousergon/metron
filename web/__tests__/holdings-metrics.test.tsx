// Holdings metrics (Holdings metrics feature): the Valuation / Fundamentals / Technicals
// column bands on HoldingsTable (Fundamentals includes balance-sheet/debt metrics as of
// metron-ops#140), and the sector → country grouping with SP1500 median bands.

import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

// HoldingsTable (client) → next/navigation + the server-actions module. Stub both.
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn() }) }));
vi.mock("@/app/portfolios/[id]/actions", () => ({
  setSecurityLabelAction: vi.fn(),
  setSecurityClassificationAction: vi.fn(),
}));

import { HoldingsTable } from "@/components/holdings-table";
import { GroupedByClassification } from "@/components/grouped-by-classification";
import type { Holding, ValuationMedians } from "@/lib/api";

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
    ...over,
  }) as Holding;

describe("HoldingsTable metric bands", () => {
  it("shows Valuation/Fundamentals/Technicals group headers + formatted metric cells when priced", () => {
    render(
      <HoldingsTable
        baseCurrency="USD"
        priced
        holdings={[
          h("AAPL", {
            pe: 30.2,
            pb: 6.0,
            rsi_14: 61,
            pct_to_ma_50: 0.05,
            market_cap: 3.0e12,
            cash: 6.0e10,
            net_debt: 5.0e10,
            net_debt_to_ebitda: 0.5,
          }),
        ]}
      />,
    );
    expect(screen.getByText("Valuation")).toBeInTheDocument();
    // Fundamentals now covers the full financial-statement picture — growth/margins AND
    // balance-sheet leverage/liquidity in one band (metron-ops#140) — so there's a single
    // "Fundamentals" header, not a separate "Balance Sheet" one.
    expect(screen.getAllByText("Fundamentals").length).toBeGreaterThan(0);
    expect(screen.queryByText("Balance Sheet")).not.toBeInTheDocument();
    expect(screen.getByText("Technicals")).toBeInTheDocument();
    expect(screen.getByText("30.2×")).toBeInTheDocument(); // P/E multiple
    expect(screen.getByText("$3.0T")).toBeInTheDocument(); // market cap
    expect(screen.getByText("+5.0%")).toBeInTheDocument(); // % above 50d MA (signed)
    expect(screen.getByText("$60.0B")).toBeInTheDocument(); // cash balance — now a Fundamentals column
    expect(screen.getByText("$50.0B")).toBeInTheDocument(); // net debt — now a Fundamentals column
  });

  it("renders — for a metric coverage gap (null), never a fabricated 0", () => {
    render(<HoldingsTable baseCurrency="USD" priced holdings={[h("AAPL", { pe: null, pb: null })]} />);
    // No P/E value rendered; the cell shows the em-dash placeholder.
    expect(screen.queryByText("30.2×")).not.toBeInTheDocument();
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
  });

  it("hides the metric bands in the cost-basis-only (unpriced) view", () => {
    render(<HoldingsTable baseCurrency="USD" priced={false} holdings={[h("AAPL")]} />);
    expect(screen.queryByText("Valuation")).not.toBeInTheDocument();
    expect(screen.queryByText("Technicals")).not.toBeInTheDocument();
  });
});

describe("GroupedByClassification", () => {
  const medians: ValuationMedians = {
    as_of: "2026-06-26",
    by_sector: {
      Technology: { n: 152, trailing_pe: 28.0, forward_pe: 24.0, price_to_book: 6.2, price_to_sales: 5.1, ev_ebitda: 18.0, dividend_yield: 0.01 },
    },
    by_country: {
      "United States": { n: 800, trailing_pe: 22.0, forward_pe: null, price_to_book: 3.5, price_to_sales: null, ev_ebitda: null, dividend_yield: 0.015 },
    },
  };

  it("groups by sector then country with the SP1500 median bands", () => {
    const holdings = [
      h("AAPL", { sector: "Technology", country: "United States" }),
      h("TSM", { sector: "Technology", country: "Taiwan" }),
    ];
    render(<GroupedByClassification holdings={holdings} baseCurrency="USD" priced medians={medians} />);

    expect(screen.getByRole("heading", { level: 3, name: /Technology/ })).toBeInTheDocument();
    // Country sub-headings (h4) — distinct from the Country column cells of the same name.
    expect(screen.getByRole("heading", { level: 4, name: "United States" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 4, name: "Taiwan" })).toBeInTheDocument();
    // Sector median band carries the SP1500 Technology median P/E (28.0×).
    expect(screen.getByText("28.0×")).toBeInTheDocument();
    // A country with no median in the artifact falls back to the no-benchmark note.
    expect(screen.getByText(/no country median benchmark available/)).toBeInTheDocument();
  });

  it("degrades gracefully when medians are unavailable (off-feed)", () => {
    render(
      <GroupedByClassification
        holdings={[h("AAPL", { sector: "Technology", country: "United States" })]}
        baseCurrency="USD"
        priced
        medians={null}
      />,
    );
    expect(screen.getByText(/no sector median benchmark available/)).toBeInTheDocument();
    expect(screen.getByText("AAPL")).toBeInTheDocument();
  });
});
