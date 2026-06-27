// Holdings Consensus band (metron-ops#105): the consensus-research + news-sentiment column
// band on HoldingsTable — rating label, mean target, target upside, # analysts, sentiment.
// Free-source data spine; each cell is null (—) off-feed or on a coverage gap.

import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

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
    last_price: 200,
    last_price_date: "2026-06-26",
    market_value_local: 2000,
    cost_basis_base: 1000,
    market_value: 2000,
    unrealized_gain: 1000,
    unrealized_pct: 1.0,
    security_type: "equity",
    sector: "Technology",
    country: "United States",
    ...over,
  }) as Holding;

describe("HoldingsTable Consensus band", () => {
  it("shows the Consensus header + rating label, target, upside, # analysts, sentiment", () => {
    render(
      <HoldingsTable
        baseCurrency="USD"
        priced
        holdings={[
          h("AAPL", {
            consensus_rating: "buy",
            consensus_score: 0.5,
            price_target_mean: 240,
            price_target_upside: 0.2,
            num_analysts: 38,
            news_sentiment: 0.31,
          }),
        ]}
      />,
    );
    expect(screen.getByText("Consensus")).toBeInTheDocument();
    expect(screen.getByText("Buy")).toBeInTheDocument();            // rating bucket → label
    expect(screen.getByText("+20.0%")).toBeInTheDocument();          // target upside (signed)
    expect(screen.getByText("38")).toBeInTheDocument();             // # analysts
    expect(screen.getByText("0.31")).toBeInTheDocument();           // news sentiment
  });

  it("renders — for a consensus coverage gap (null), never a fabricated value", () => {
    render(
      <HoldingsTable
        baseCurrency="USD"
        priced
        holdings={[h("ZZZ", { consensus_rating: null, news_sentiment: null, num_analysts: null })]}
      />,
    );
    expect(screen.queryByText("Buy")).not.toBeInTheDocument();
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
  });

  it("hides the Consensus band in the cost-basis-only (unpriced) view", () => {
    render(<HoldingsTable baseCurrency="USD" priced={false} holdings={[h("AAPL")]} />);
    expect(screen.queryByText("Consensus")).not.toBeInTheDocument();
  });
});
