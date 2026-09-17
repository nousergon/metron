// Glance screen render at the canonical 390 px phone width (metron-ops#248): six zones in
// order, per-row provenance badges, the integrity zone on the healthy path, the quiet-day
// floor, feed-off "not available" copy, and one-tap links to the computing surface.

import { beforeAll, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn() }) }));

import { GlanceScreen } from "@/components/glance-screen";
import type { Glance } from "@/lib/api-glance";

const PID = "11111111-1111-1111-1111-111111111111";

const base: Glance = {
  portfolio_id: PID,
  generated_at: "2026-09-15T22:00:00+00:00",
  state: "post_close",
  tier: "beta",
  feed_enabled: false,
  headline: {
    base_currency: "USD",
    total_value: 125000,
    market_value: 120000,
    cash: 5000,
    value_as_of: "2026-09-15",
    value_provenance: "as_of_sync",
    last_sync: "2026-09-15",
    day_change: 850,
    day_pct: 0.0068,
    day_as_of: "2026-09-15",
    day_provenance: "settled",
    day_note: null,
    n_accounts: 3,
    n_brokers: 2,
    surface: "overview",
  },
  path: {
    available: true,
    reason: null,
    points: [
      { date: "2026-09-01", nav: 120000 },
      { date: "2026-09-15", nav: 125000 },
    ],
    as_of: "2026-09-15",
    provenance: "settled",
    state: "post_close",
    intraday_points: [],
    surface: "performance",
  },
  movers: {
    key: "movers",
    items: [
      {
        facet_key: "movement_decomposition",
        family: "movement",
        label: "What moved the portfolio today",
        text: "AAPL moved the portfolio +$600 in the session ending 2026-09-15.",
        as_of: "2026-09-15",
        provenance: "settled",
        surface: "overview",
        score: 1,
        ticker: "AAPL",
        amount: 600,
        pct: 0.012,
        event_date: null,
      },
    ],
    is_floor: false,
    floor_text: null,
    as_of: "2026-09-15",
    unavailable: [],
  },
  insights: {
    key: "insights",
    items: [],
    is_floor: true,
    floor_text: "Nothing unusual today — your portfolio moved with its factors.",
    as_of: "2026-09-15",
    unavailable: [],
  },
  ahead: {
    key: "ahead",
    items: [
      {
        facet_key: "long_term_boundary",
        family: "tax",
        label: "Lots approaching the one-year boundary",
        text: "10 shares of MSFT reach a long-term holding period on 2026-09-30.",
        as_of: "2026-09-15",
        provenance: "as_of_sync",
        surface: "tax",
        score: 0.5,
        ticker: "MSFT",
        amount: 120,
        pct: null,
        event_date: "2026-09-30",
      },
    ],
    is_floor: false,
    floor_text: null,
    as_of: "2026-09-15",
    unavailable: ["Upcoming earnings for your holdings"],
  },
  integrity: {
    status: "reconciled",
    healthy: true,
    text: "Reconciled as of Sep 15, 09:32 ET.",
    open_breaks: 0,
    last_reconciled_at: "2026-09-15T13:32:00+00:00",
    positions_as_of: "2026-09-15",
    brokers: ["ibkr_flex", "snaptrade"],
    as_of: "2026-09-15T22:00:00+00:00",
    surface: "diagnostics",
  },
  coverage: { candidate_facets: 30, produced_facets: 5, observations: 4 },
  degraded: [],
  timings_ms: {},
};

describe("GlanceScreen at 390 px", () => {
  beforeAll(() => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 390 });
    window.dispatchEvent(new Event("resize"));
  });

  it("renders the six zones in order", () => {
    const { container } = render(<GlanceScreen g={base} portfolioId={PID} />);
    const zones = Array.from(container.querySelectorAll("[data-zone]")).map((z) => z.getAttribute("data-zone"));
    expect(zones).toEqual(["headline", "path", "movers", "insights", "ahead", "integrity"]);
    expect(container.firstElementChild).toHaveClass("w-full");
    expect(screen.getByText("After the close")).toBeInTheDocument();
  });

  it("carries a provenance badge on the headline and every ranked row", () => {
    const { container } = render(<GlanceScreen g={base} portfolioId={PID} />);
    expect(screen.getByText("$125,000")).toBeInTheDocument();
    expect(screen.getByText("3 accounts · 2 brokers")).toBeInTheDocument();
    const badges = Array.from(container.querySelectorAll("[data-provenance]")).map((b) => b.getAttribute("data-provenance"));
    expect(badges).toContain("settled");
    expect(badges).toContain("as_of_sync");
    // movers row + ahead row each carry one
    expect(container.querySelectorAll("[data-facet] [data-provenance]").length).toBe(2);
  });

  it("renders integrity on the healthy path, the floor, and not-available copy", () => {
    const { container } = render(<GlanceScreen g={base} portfolioId={PID} />);
    expect(screen.getByText("Reconciled as of Sep 15, 09:32 ET.")).toBeInTheDocument();
    expect(container.querySelector('[data-floor="true"]')).toHaveTextContent("Nothing unusual today");
    expect(screen.getByText(/Not available on this plan: Upcoming earnings/)).toBeInTheDocument();
  });

  it("taps once to the surface that computed each claim", () => {
    const { container } = render(<GlanceScreen g={base} portfolioId={PID} />);
    expect(container.querySelector('[data-facet="long_term_boundary"]')).toHaveAttribute("href", `/portfolios/${PID}/tax`);
    expect(container.querySelector('[data-status="reconciled"]')).toHaveAttribute("href", `/portfolios/${PID}/diagnostics`);
  });

  it("uses no directive wording", () => {
    const { container } = render(<GlanceScreen g={base} portfolioId={PID} />);
    expect(container.textContent ?? "").not.toMatch(/\bshould\b|\bwe recommend\b|\brecommended\b/i);
  });
});
