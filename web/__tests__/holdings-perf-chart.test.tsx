// HoldingsPerfChart (metron-ops#78) — the re-basing math the chart depends on, plus the
// render paths that would regress silently (range buttons, benchmark toggle, feed-gated
// no-overlay path, empty state).

import { describe, expect, it } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

import { HoldingsPerfChart, rebase } from "@/components/holdings-perf-chart";
import type { AccountSeries, BenchmarkSeries, SeriesPoint } from "@/lib/api";

const pts = (xs: [string, number][]): SeriesPoint[] => xs.map(([when, g]) => ({ when, g }));

describe("rebase", () => {
  it("re-bases the full series to 100 at its first point", () => {
    const r = rebase(pts([["2024-01-01", 1], ["2024-02-01", 1.1], ["2024-03-01", 1.21]]), null);
    expect(r.map((p) => Math.round(p.v))).toEqual([100, 110, 121]);
  });

  it("filters to the window cutoff and re-bases to 100 at the first in-window point", () => {
    const r = rebase(pts([["2024-01-01", 1], ["2024-02-01", 1.1], ["2024-03-01", 1.21]]), "2024-02-01");
    // window = [1.1, 1.21] → re-based to 100, 110.
    expect(r.map((p) => Math.round(p.v))).toEqual([100, 110]);
  });

  it("returns nothing when fewer than two points fall in the window", () => {
    expect(rebase(pts([["2024-01-01", 1], ["2024-02-01", 1.1]]), "2024-02-01")).toEqual([]);
  });
});

const accounts: AccountSeries[] = [
  { account_id: "a1", name: "Brokerage", points: pts([["2024-01-01", 1], ["2024-02-01", 1.1], ["2024-03-01", 1.2]]) },
  { account_id: "a2", name: "IRA", points: pts([["2024-01-01", 1], ["2024-02-01", 0.95], ["2024-03-01", 1.05]]) },
];
const benchmarks: BenchmarkSeries[] = [
  { symbol: "SPY", label: "S&P 500", points: pts([["2024-01-01", 1], ["2024-02-01", 1.05], ["2024-03-01", 1.08]]) },
];

describe("HoldingsPerfChart", () => {
  it("renders a legend entry per account plus the toggled-on benchmark", () => {
    render(<HoldingsPerfChart accounts={accounts} benchmarks={benchmarks} benchmarksAvailable />);
    expect(screen.getByText("Brokerage")).toBeTruthy();
    expect(screen.getByText("IRA")).toBeTruthy();
    // SPY appears as both a toggle chip and a legend label.
    expect(screen.getAllByText("SPY").length).toBeGreaterThanOrEqual(2);
  });

  it("drops the benchmark overlay when its chip is toggled off", () => {
    render(<HoldingsPerfChart accounts={accounts} benchmarks={benchmarks} benchmarksAvailable />);
    fireEvent.click(screen.getByRole("button", { name: "SPY" }));
    // Toggle chip remains; the legend label is gone (overlay hidden).
    expect(screen.getAllByText("SPY").length).toBe(1);
  });

  it("shows a Pro hint and no benchmark chips when benchmarks are unavailable", () => {
    render(<HoldingsPerfChart accounts={accounts} benchmarks={[]} benchmarksAvailable={false} />);
    expect(screen.getByText("Benchmarks: Pro")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "SPY" })).toBeNull();
  });

  it("shows the empty message when no account has enough history for the range", () => {
    const thin: AccountSeries[] = [{ account_id: "a1", name: "Brokerage", points: pts([["2024-01-01", 1]]) }];
    render(<HoldingsPerfChart accounts={thin} benchmarks={[]} benchmarksAvailable={false} />);
    expect(screen.getByText("Not enough history yet for this range.")).toBeTruthy();
  });
});
