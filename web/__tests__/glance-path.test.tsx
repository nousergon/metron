// GlancePath (metron-ops-I324) — zone 2 renders the OPEN-state intraday sparkline when
// a live series is reachable (never a settled path masquerading as today), falls back to
// the settled path + provenance badge when it isn't, and never mixes the two point
// arrays into one line.

import { describe, expect, it } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

import { GlancePath, polylinePoints } from "@/components/glance-path";
import type { GlancePath as GlancePathZone } from "@/lib/api-glance";

describe("polylinePoints", () => {
  it("returns nothing for fewer than two points", () => {
    expect(polylinePoints([100])).toBe("");
    expect(polylinePoints([])).toBe("");
  });

  it("scales two points across the full width", () => {
    const d = polylinePoints([100, 110]);
    const [p0, p1] = d.split(" ");
    expect(p0!.startsWith("0.00,")).toBe(true);
    expect(p1!.startsWith("100.00,")).toBe(true);
  });
});

const settledPoints = [
  { date: "2026-08-15", nav: 100000 },
  { date: "2026-09-15", nav: 105000 },
];

function pathFixture(overrides: Partial<GlancePathZone>): GlancePathZone {
  return {
    available: true,
    reason: null,
    points: settledPoints,
    as_of: "2026-09-15",
    provenance: "settled",
    state: "post_close",
    intraday_points: [],
    surface: "performance",
    ...overrides,
  };
}

describe("GlancePath", () => {
  it("open + live reachable: shows the intraday sparkline with a live badge, not the settled path", () => {
    const path = pathFixture({
      state: "open",
      provenance: "live",
      as_of: "2026-09-16T15:00:00Z",
      intraday_points: [
        { as_of: "2026-09-15", nav: 105000 },
        { as_of: "2026-09-16T15:00:00Z", nav: 105450 },
      ],
    });
    render(<GlancePath path={path} href="/portfolios/p1/performance" />);
    expect(screen.getByText(/today/i)).toBeTruthy();
    expect(screen.getByText(/live/i)).toBeTruthy();
    expect(screen.getByRole("img", { name: "Today's intraday NAV path" })).toBeTruthy();
  });

  it("open + live reachable: tapping the sparkline reveals the settled history, never merging the arrays", () => {
    const path = pathFixture({
      state: "open",
      provenance: "live",
      as_of: "2026-09-16T15:00:00Z",
      intraday_points: [
        { as_of: "2026-09-15", nav: 105000 },
        { as_of: "2026-09-16T15:00:00Z", nav: 105450 },
      ],
    });
    render(<GlancePath path={path} href="/portfolios/p1/performance" />);
    fireEvent.click(screen.getByRole("button"));
    expect(screen.getByRole("img", { name: "NAV path" })).toBeTruthy();
    expect(screen.queryByRole("img", { name: "Today's intraday NAV path" })).toBeNull();
  });

  it("open + no live series: falls back to the settled path and names why via the reason text", () => {
    const path = pathFixture({
      state: "open",
      provenance: "settled",
      reason: "Live intraday path not available; showing the last settled session.",
      intraday_points: [],
    });
    render(<GlancePath path={path} href="/portfolios/p1/performance" />);
    expect(screen.getByText(/not available/i)).toBeTruthy();
    expect(screen.getByText("settled", { exact: false, selector: "[data-provenance]" })).toBeTruthy();
    expect(screen.getByRole("img", { name: "NAV path" })).toBeTruthy();
  });

  it("post_close: renders the settled path exactly as before, no intraday affordance", () => {
    const path = pathFixture({ state: "post_close" });
    render(<GlancePath path={path} href="/portfolios/p1/performance" />);
    expect(screen.queryByText(/today · tap/i)).toBeNull();
    expect(screen.getByRole("img", { name: "NAV path" })).toBeTruthy();
  });

  it("not available and no settled history either: renders the reason, not a chart", () => {
    const path = pathFixture({ available: false, reason: "Not enough recorded history to draw a path yet.", points: [] });
    render(<GlancePath path={path} href="/portfolios/p1/performance" />);
    expect(screen.getByText("Not enough recorded history to draw a path yet.")).toBeTruthy();
    expect(screen.queryByRole("img")).toBeNull();
  });
});
