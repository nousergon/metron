// Holdings Attractiveness band: SOTA 6-pillar cross-sectional score + pillar columns.

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

describe("HoldingsTable Attractiveness band", () => {
  it("shows the headline score + six pillar columns", () => {
    render(
      <HoldingsTable
        baseCurrency="USD"
        priced
        holdings={[
          h("AAPL", {
            attractiveness: 72.4,
            attractiveness_coverage: 6,
            attractiveness_quality: 90,
            attractiveness_value: 30,
            attractiveness_momentum: 85,
            attractiveness_growth: 80,
            attractiveness_stewardship: 70,
            attractiveness_defensiveness: 60,
          }),
        ]}
        visibleBands={["Attractiveness"]}
      />,
    );
    expect(screen.getAllByText("Attractiveness").length).toBeGreaterThan(0);
    expect(screen.getByText("Factor score")).toBeInTheDocument();
    expect(screen.getByText("72.4")).toBeInTheDocument();
    expect(screen.getByText("90")).toBeInTheDocument();
    expect(screen.getByText("30")).toBeInTheDocument();
    expect(screen.getByText("85")).toBeInTheDocument();
  });

  it("renders — for a coverage gap, never a fabricated value", () => {
    render(
      <HoldingsTable
        baseCurrency="USD"
        priced
        holdings={[
          h("ZZZ", {
            attractiveness: null,
            attractiveness_coverage: null,
            attractiveness_quality: null,
            attractiveness_value: null,
            attractiveness_momentum: null,
            attractiveness_growth: null,
            attractiveness_stewardship: null,
            attractiveness_defensiveness: null,
          }),
        ]}
        visibleBands={["Attractiveness"]}
      />,
    );
    expect(screen.getAllByText("Attractiveness").length).toBeGreaterThan(0);
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
  });

  it("carries the factor-profile as-of as a per-row hover title (P-28)", () => {
    render(
      <HoldingsTable
        baseCurrency="USD"
        priced
        holdings={[h("AAPL", { attractiveness: 72.4, attractiveness_as_of: "2026-09-10" })]}
        visibleBands={["Attractiveness"]}
      />,
    );
    expect(screen.getByText("72.4").title).toBe("Factor profiles as of 2026-09-10");
  });
});

// metron-ops-I334 — the factor-profile substrate's stale/retired states render explicitly,
// and both stay visually distinct from a per-ticker coverage gap ("—").
describe("HoldingsTable Attractiveness band — stale / retired substrate", () => {
  const attractivenessCells = (container: HTMLElement) =>
    Array.from(container.querySelectorAll("[data-factor-state]"));

  it("tags a stale score with a muted 'stale' marker rather than the bare number", () => {
    render(
      <HoldingsTable
        baseCurrency="USD"
        priced
        holdings={[
          h("AAPL", {
            attractiveness: 72.4,
            attractiveness_quality: 90,
            attractiveness_as_of: "2026-09-01",
            attractiveness_stale: true,
            attractiveness_retired: false,
          }),
        ]}
        visibleBands={["Attractiveness"]}
      />,
    );
    const score = screen.getByText("72.4");
    expect(score.nextElementSibling?.textContent).toBe("stale");
    expect(screen.getAllByText("stale").length).toBeGreaterThanOrEqual(2); // headline + Qual pillar
    expect(screen.queryByText("retired")).not.toBeInTheDocument();
  });

  it("renders a fresh score with no stale tag", () => {
    render(
      <HoldingsTable
        baseCurrency="USD"
        priced
        holdings={[h("AAPL", { attractiveness: 72.4, attractiveness_stale: false, attractiveness_retired: false })]}
        visibleBands={["Attractiveness"]}
      />,
    );
    expect(screen.getByText("72.4")).toBeInTheDocument();
    expect(screen.queryByText("stale")).not.toBeInTheDocument();
  });

  it("reads 'retired' in every Attractiveness column once the substrate is retired — never blank or —", () => {
    const { container } = render(
      <HoldingsTable
        baseCurrency="USD"
        priced
        holdings={[h("AAPL", { attractiveness: null, attractiveness_stale: false, attractiveness_retired: true })]}
        visibleBands={["Attractiveness"]}
      />,
    );
    const retired = screen.getAllByText("retired");
    expect(retired).toHaveLength(7); // Factor score + six pillars
    expect(retired[0].title).toMatch(/retired/i);
    expect(screen.queryByText("—")).not.toBeInTheDocument();
    expect(attractivenessCells(container).every((el) => el.getAttribute("data-factor-state") === "retired")).toBe(true);
  });

  it("keeps a coverage gap as — with neither a stale nor a retired marker", () => {
    const { container } = render(
      <HoldingsTable
        baseCurrency="USD"
        priced
        holdings={[h("ZZZ", { attractiveness: null, attractiveness_stale: false, attractiveness_retired: false })]}
        visibleBands={["Attractiveness"]}
      />,
    );
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
    expect(attractivenessCells(container)).toHaveLength(0);
  });

  it("leaves the Technical attractiveness column untouched when the factor substrate is retired", () => {
    render(
      <HoldingsTable
        baseCurrency="USD"
        priced
        holdings={[
          h("AAPL", {
            attractiveness: null,
            attractiveness_retired: true,
            tech_rating_score: 0.8,
            tech_rating_label: "Strong Buy",
          }),
        ]}
        visibleBands={["Technicals"]}
      />,
    );
    expect(screen.getByText("Strong Buy")).toBeInTheDocument();
    expect(screen.queryByText("retired")).not.toBeInTheDocument();
  });
});
