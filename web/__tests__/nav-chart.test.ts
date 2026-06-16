// scaleSeries — the pure NAV→SVG-coord mapping behind the Performance chart
// (metron-ops#44). y is inverted (higher value = smaller y / higher on screen).

import { describe, expect, it } from "vitest";
import { scaleSeries } from "@/components/nav-chart";

describe("scaleSeries", () => {
  it("spreads points evenly across the inner width and inverts y", () => {
    const pts = scaleSeries([10, 20, 30], 100, 100, 10); // inner 80×80, x in [10,90]
    expect(pts.map((p) => p.x)).toEqual([10, 50, 90]);
    // min (10) sits at the bottom (y = h-pad = 90), max (30) at the top (y = pad = 10).
    expect(pts[0].y).toBeCloseTo(90);
    expect(pts[2].y).toBeCloseTo(10);
    expect(pts[1].y).toBeCloseTo(50);
  });

  it("a flat series rides the vertical midline", () => {
    const pts = scaleSeries([5, 5, 5], 100, 100, 10);
    expect(pts.every((p) => Math.abs(p.y - 50) < 1e-9)).toBe(true);
  });

  it("a single point centers horizontally", () => {
    const pts = scaleSeries([7], 100, 100, 10);
    expect(pts).toHaveLength(1);
    expect(pts[0].x).toBeCloseTo(50);
  });

  it("returns nothing for an empty series", () => {
    expect(scaleSeries([])).toEqual([]);
  });
});
