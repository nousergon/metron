// ReconciliationSection — the sector attribution against the observed returns over the
// same window (metron-ops-I372). The residual is shown next to the effects, never hidden,
// and an unavailable observed figure is named.

import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import type { AttributionReconciliation } from "@/lib/api";
import { ReconciliationSection } from "@/components/attribution-reconciliation";

const BASE: AttributionReconciliation = {
  available: true,
  reason: null,
  observed_portfolio_return: 0.05,
  observed_benchmark_return: 0.03,
  observed_active_return: 0.02,
  portfolio_residual: 0.01,
  benchmark_residual: -0.005,
  active_residual: 0.015,
  within_tolerance: false,
  tolerance: 0.0005,
};

describe("ReconciliationSection", () => {
  it("shows the observed returns and the named residuals", () => {
    render(<ReconciliationSection rec={BASE} benchmark="SPY" />);
    expect(screen.getByText("Reconciliation")).toBeTruthy();
    expect(screen.getByText(/current-holdings analysis, not realized attribution/)).toBeTruthy();
    expect(screen.getByText(/does not tie to the observed active return within 5 bps/)).toBeTruthy();
    expect(screen.getByText("Portfolio (observed TWR vs attributed holdings)")).toBeTruthy();
    expect(screen.getByText("SPY (observed vs sector-ETF composite)")).toBeTruthy();
    expect(screen.getByText("Active return")).toBeTruthy();
  });

  it("says when it ties", () => {
    render(<ReconciliationSection rec={{ ...BASE, active_residual: 0.0001, within_tolerance: true }} benchmark="QQQ" />);
    expect(screen.getByText(/It ties to the observed active return\./)).toBeTruthy();
  });

  it("names why it is not reconciled and shows a dash for what is missing", () => {
    render(
      <ReconciliationSection
        rec={{
          ...BASE,
          available: false,
          reason: "no NAV snapshot on 2024-02-19",
          observed_portfolio_return: null,
          observed_active_return: null,
          portfolio_residual: null,
          active_residual: null,
        }}
        benchmark="SPY"
      />,
    );
    expect(screen.getByText(/Not fully reconciled: no NAV snapshot on 2024-02-19\./)).toBeTruthy();
    expect(screen.getAllByText("—").length).toBe(4);
  });
});
