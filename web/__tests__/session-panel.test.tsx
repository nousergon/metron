// SessionPanel / SessionNotes (Holdings live-session view) — the landing-page layout pass
// (Brian, 2026-09-28): the headline panel carries only the coverage banner + strip, the
// explanatory footnotes render separately (the page puts them at the bottom), the coverage
// share is unsigned, and the "as of" time is formatted client-side in the viewer's zone.

import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { renderToString } from "react-dom/server";
import { SessionNotes, SessionPanel } from "@/components/session-panel";
import { LocalAsOf, asOfLabel } from "@/components/local-as-of";
import type { IntradayLegHistory, IntradayStatus, Today } from "@/lib/api";

const AS_OF = "2026-09-28T15:20:00Z";

const status: IntradayStatus = {
  applied: true,
  as_of_utc: AS_OF,
  stale: false,
  n_priced: 40,
  n_total: 50,
  n_estimated: 0,
  reason: null,
  covered_nav: 767333,
  total_nav: 840449,
  sources: {},
  session_state: "live",
};

const today = {
  available: true,
  base_currency: "USD",
  reason: null,
  as_of_utc: AS_OF,
  stale: false,
  n_priced: 40,
  n_excluded: 1,
  overnight_gain: 100,
  intraday_gain: 200,
  day_gain: 300,
  overnight_pct: 0.001,
  intraday_pct: 0.002,
  day_pct: 0.003,
  covered_prev_mv: 771547,
  rows: [{ ticker: "AAPL" }],
  excluded_rows: [{ ticker: "1299", label: "AIA", reason: "no_quote" }],
} as unknown as Today;

const legs: IntradayLegHistory = {
  days: [],
  cum_overnight_pct: 0.01,
  cum_intraday_pct: -0.005,
  cum_day_pct: 0.005,
  n_days: 82,
};

describe("SessionPanel", () => {
  it("renders the coverage share unsigned — a share, not a change", () => {
    render(<SessionPanel status={status} today={today} ccy="USD" />);
    expect(screen.getByText(/Live session covers/)).toHaveTextContent("NAV (91.3%)");
    expect(screen.getByText(/Live session covers/)).not.toHaveTextContent("+91.3%");
  });

  it("keeps the footnotes out of the headline panel", () => {
    render(<SessionPanel status={status} today={today} ccy="USD" />);
    expect(screen.getByText("Overnight")).toBeInTheDocument();
    expect(screen.queryByText(/Not in the live session/)).not.toBeInTheDocument();
    expect(screen.queryByText(/over the covered basis/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Since tracking/)).not.toBeInTheDocument();
  });
});

describe("SessionNotes", () => {
  it("renders the covered-basis, excluded-holdings and drift-split footnotes", () => {
    render(<SessionNotes today={today} legs={legs} ccy="USD" />);
    expect(screen.getByText(/over the covered basis/)).toHaveTextContent("$771,547");
    expect(screen.getByText(/Not in the live session \(1\)/)).toHaveTextContent("AIA (no live quote)");
    expect(screen.getByText(/Since tracking \(82 days\)/)).toBeInTheDocument();
  });

  it("renders nothing when there is no session to annotate", () => {
    const { container } = render(<SessionNotes today={{ ...today, available: false }} legs={legs} ccy="USD" />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("LocalAsOf", () => {
  it("never formats on the server (whose zone is not the viewer's)", () => {
    expect(renderToString(<LocalAsOf iso={AS_OF} prefix=" · as of " />)).toBe("");
  });

  it("formats in the viewer's local zone once mounted — the header's convention", () => {
    render(<LocalAsOf iso={AS_OF} prefix="as of " />);
    const local = new Date(AS_OF).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
    expect(screen.getByText(new RegExp(`as of .*${local.replace(/\s/g, "\\s")}`))).toBeInTheDocument();
  });

  it("adds the day once the snapshot is from an earlier local day", () => {
    const now = new Date(new Date(AS_OF).getTime() + 3 * 24 * 3600 * 1000);
    expect(asOfLabel(AS_OF, now)).toMatch(/^\w{3}, \w{3} \d{1,2} · /);
    expect(asOfLabel(AS_OF, new Date(AS_OF))).not.toContain("·");
    expect(asOfLabel(null)).toBe("");
    expect(asOfLabel("not a date")).toBe("");
  });
});
