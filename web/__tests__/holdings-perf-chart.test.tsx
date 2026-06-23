// HoldingsPerfChart (metron-ops#78) — the re-basing math the chart depends on, plus the
// render paths that would regress silently (range buttons, benchmark toggle, feed-gated
// no-overlay path, empty state).

import { describe, expect, it } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

import { HoldingsPerfChart, hoverRows, rebase, valueAt } from "@/components/holdings-perf-chart";
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

describe("hover readout", () => {
  const lines = [
    { key: "a1", label: "Brokerage", color: "#000", dashed: false, points: [
      { when: "2024-01-01", v: 100 }, { when: "2024-02-01", v: 110 }, { when: "2024-03-01", v: 120 },
    ] },
    { key: "a2", label: "IRA", color: "#111", dashed: false, points: [
      { when: "2024-01-01", v: 100 }, { when: "2024-02-01", v: 95 }, { when: "2024-03-01", v: 105 },
    ] },
  ];

  it("valueAt snaps to the nearest sample by date", () => {
    expect(valueAt(lines[0]!.points, Date.parse("2024-02-03"))?.when).toBe("2024-02-01");
    expect(valueAt(lines[0]!.points, Date.parse("2024-02-20"))?.when).toBe("2024-03-01");
    expect(valueAt([], 0)).toBeNull();
  });

  it("hoverRows returns each line's % return at the cursor, sorted high→low", () => {
    const rows = hoverRows(lines, Date.parse("2024-03-01"));
    expect(rows.map((r) => r.label)).toEqual(["Brokerage", "IRA"]); // +20% before +5%
    expect(rows.map((r) => Math.round(r.pct))).toEqual([20, 5]);
  });

  it("hoverRows re-sorts as the leader changes earlier in the window", () => {
    // At Feb, IRA is −5% and Brokerage +10% → Brokerage still leads.
    const feb = hoverRows(lines, Date.parse("2024-02-01"));
    expect(feb.map((r) => r.label)).toEqual(["Brokerage", "IRA"]);
    expect(feb.map((r) => Math.round(r.pct))).toEqual([10, -5]);
  });
});
