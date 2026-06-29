// PerfTiles — the Overview performance-vs-market hero (metron-ops#83). Guards the
// behaviors that would regress silently: benchmark toggles add/remove the alpha rows,
// the no-feed (benchmarksAvailable=false) path renders portfolio-only with a Pro hint,
// and a window with no data shows "history building" instead of a fabricated zero.

import { describe, expect, it } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

import { PerfTiles } from "@/components/perf-tiles";
import type { PeriodTile } from "@/lib/api";

const bench = (symbol: string, label: string, ret: number, alpha: number) => ({ symbol, label, ret, alpha });

const tiles: PeriodTile[] = [
  {
    period: "today",
    label: "Today",
    start_date: "2024-06-29",
    end_date: "2024-06-30",
    gain: 20,
    twr: 0.015,
    benchmarks: [bench("SPY", "S&P 500", 0.008, 0.007)],
  },
  {
    period: "ytd",
    label: "YTD",
    start_date: "2023-12-31",
    end_date: "2024-06-30",
    gain: 220,
    twr: 0.2,
    benchmarks: [bench("SPY", "S&P 500", 0.1, 0.1), bench("QQQ", "Nasdaq 100", 0.15, 0.05)],
  },
  {
    period: "ltm",
    label: "LTM",
    start_date: "2023-06-01",
    end_date: "2024-06-30",
    gain: 320,
    twr: 0.32,
    benchmarks: [bench("SPY", "S&P 500", 0.21, 0.11)],
  },
];

describe("PerfTiles", () => {
  it("renders all three windows with $ gain + %TWR", () => {
    render(<PerfTiles tiles={tiles} benchmarksAvailable />);
    expect(screen.getByText("Today")).toBeTruthy();
    expect(screen.getByText("YTD")).toBeTruthy();
    expect(screen.getByText("LTM")).toBeTruthy();
    expect(screen.getByText("+1.5% TWR")).toBeTruthy(); // today TWR 0.015 → +1.5%
    expect(screen.getByText("+20.0% TWR")).toBeTruthy(); // YTD TWR 0.20 → +20.0%
  });

  it("shows benchmark alpha rows and toggles them off when the chip is deselected", () => {
    render(<PerfTiles tiles={tiles} benchmarksAvailable />);
    // SPY alpha rows are present on all three tiles by default.
    expect(screen.getAllByText("vs S&P 500").length).toBe(3);
    // Deselect SPY → its alpha rows disappear; QQQ (on YTD) remains.
    fireEvent.click(screen.getByRole("button", { name: "SPY" }));
    expect(screen.queryAllByText("vs S&P 500").length).toBe(0);
    expect(screen.getAllByText("vs Nasdaq 100").length).toBe(1);
  });

  it("renders portfolio-only with a Pro hint when benchmarks are unavailable", () => {
    render(<PerfTiles tiles={tiles} benchmarksAvailable={false} />);
    expect(screen.getByText("Benchmarks: Pro")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "SPY" })).toBeNull();
    expect(screen.queryByText("vs S&P 500")).toBeNull();
  });

  it("shows 'history building' for a window with no data", () => {
    const building: PeriodTile[] = [
      { period: "today", label: "Today", start_date: null, end_date: null, gain: null, twr: null, benchmarks: [] },
      ...tiles.slice(1),
    ];
    render(<PerfTiles tiles={building} benchmarksAvailable={false} />);
    expect(screen.getByText("history building…")).toBeTruthy();
  });

  it("shows the latest close value with an as-of label when TODAY predates today", () => {
    // Intraday off / pre-open: the server populates TODAY from the latest settled close and
    // supplies an "as of <date>" note. The tile shows the value AND the label — so it's never
    // read as a live "today" move, but it's not blank either.
    const asOf: PeriodTile[] = [
      {
        period: "today",
        label: "Today",
        start_date: "2026-06-24",
        end_date: "2026-06-25",
        gain: 20,
        twr: 0.015,
        benchmarks: [],
        note: "as of 2026-06-25",
      },
      ...tiles.slice(1),
    ];
    render(<PerfTiles tiles={asOf} benchmarksAvailable={false} />);
    expect(screen.getByText("as of 2026-06-25")).toBeTruthy(); // honest label
    expect(screen.getByText("+1.5% TWR")).toBeTruthy(); // value shown, not blank
    expect(screen.queryByText("history building…")).toBeNull();
  });

  it("marks the TODAY tile live when it's the intraday number", () => {
    const live: PeriodTile[] = [{ ...tiles[0], intraday: true }, ...tiles.slice(1)];
    render(<PerfTiles tiles={live} benchmarksAvailable />);
    expect(screen.getByText("live · ~15m delay")).toBeTruthy();
  });
});
