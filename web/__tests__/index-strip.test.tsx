// IndexStrip — the Overview "markets" row (SPY/QQQ/IWM intraday proxies). Renders the
// index labels + levels + signed change% from the server-fetched `initial`, and returns
// null when the snapshot is unavailable/empty (the strip is hidden, not shown broken).
// The 5-min poll action is mocked away (we assert first-paint, not timer behavior).

import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

import type { Indices } from "@/lib/api";

vi.mock("@/app/portfolios/[id]/markets-action", () => ({
  fetchIndicesAction: vi.fn().mockResolvedValue(null),
}));

import { IndexStrip } from "@/components/index-strip";

const AVAILABLE: Indices = {
  available: true,
  reason: null,
  required_tier: null,
  as_of_utc: "2026-06-12T15:00:00Z",
  stale: false,
  indices: [
    { symbol: "SPY", label: "S&P 500", last: 605.2, prev_close: 602.4, open: 603.0, change: 2.8, change_pct: 0.00465, session_date: "2026-06-12", suspect: false, mtd_pct: 0.034, ytd_pct: 0.123, ltm_pct: 0.21 },
    { symbol: "IWM", label: "Russell 2000", last: 215.3, prev_close: 216.5, open: 216.0, change: -1.2, change_pct: -0.00554, session_date: "2026-06-12", suspect: false, ytd_pct: -0.045, ltm_pct: null },
  ],
};

describe("IndexStrip", () => {
  it("renders index labels and signed change%", () => {
    render(<IndexStrip initial={AVAILABLE} />);
    expect(screen.getByText("S&P 500")).toBeInTheDocument();
    expect(screen.getByText("Russell 2000")).toBeInTheDocument();
    expect(screen.getByText("605.20")).toBeInTheDocument();
    expect(screen.getByText("+0.5%")).toBeInTheDocument();   // up index (Today)
    expect(screen.getByText("−0.6%")).toBeInTheDocument();   // down index keeps its sign
  });

  it("renders Today / MTD / YTD / LTM period returns per index (metron-ops#87)", () => {
    render(<IndexStrip initial={AVAILABLE} />);
    expect(screen.getAllByText("Today").length).toBe(2);
    expect(screen.getAllByText("MTD").length).toBe(2);
    expect(screen.getByText("+3.4%")).toBeInTheDocument(); // SPY MTD
    expect(screen.getAllByText("YTD").length).toBe(2);
    expect(screen.getAllByText("LTM").length).toBe(2);
    expect(screen.getByText("+12.3%")).toBeInTheDocument(); // SPY YTD
    expect(screen.getByText("+21.0%")).toBeInTheDocument(); // SPY LTM
    expect(screen.getByText("−4.5%")).toBeInTheDocument();  // IWM YTD
  });

  it("renders nothing when unavailable", () => {
    const { container } = render(
      <IndexStrip initial={{ ...AVAILABLE, available: false, indices: [] }} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing when available but no quotes", () => {
    const { container } = render(<IndexStrip initial={{ ...AVAILABLE, indices: [] }} />);
    expect(container).toBeEmptyDOMElement();
  });
});
