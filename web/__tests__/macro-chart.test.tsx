// MacroChart — per-indicator card on the Macro detail page. Renders the label, latest
// value (with units) and a 1-year SVG line when there are >=2 points; degrades to a
// "not enough history" note otherwise. Carries the indicator key as the in-page anchor
// id for the Overview tile click-through.

import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import type { MacroIndicator } from "@/lib/api";
import { MacroChart } from "@/components/macro-chart";

const ind = (over: Partial<MacroIndicator> = {}): MacroIndicator => ({
  key: "DGS10",
  label: "10Y Treasury",
  units: "%",
  latest_value: 4.25,
  latest_date: "2026-06-17",
  prior_value: 4.2,
  change: 0.05,
  // API order is most-recent-first.
  history: [
    { obs_date: "2026-06-17", value: 4.25 },
    { obs_date: "2026-03-17", value: 4.1 },
    { obs_date: "2025-12-17", value: 3.9 },
  ],
  ...over,
});

describe("MacroChart", () => {
  it("renders label, value with units, and the SVG line", () => {
    const { container } = render(<MacroChart ind={ind()} />);
    expect(screen.getByText("10Y Treasury")).toBeInTheDocument();
    expect(screen.getByText("4.25%")).toBeInTheDocument();
    expect(container.querySelector("svg polyline")).not.toBeNull();
    expect(container.querySelector("#DGS10")).not.toBeNull(); // anchor for tile click-through
  });

  it("shows a note instead of a chart when history is too short", () => {
    const { container } = render(<MacroChart ind={ind({ history: [{ obs_date: "2026-06-17", value: 4.25 }] })} />);
    expect(screen.getByText(/Not enough history/)).toBeInTheDocument();
    expect(container.querySelector("svg")).toBeNull();
  });
});
