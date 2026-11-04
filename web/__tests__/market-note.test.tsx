// MarketNoteView — the Holdings "portfolio vs market" note. Renders the API's sentences
// as one paragraph, names what was left out, and says why when there is no note.

import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import type { MarketNote } from "@/lib/api";
import { MarketNoteView } from "@/components/market-note";

const NOTE: MarketNote = {
  available: true,
  session_date: "2026-11-03",
  sentences: [
    "On Tuesday Nov 3 your portfolio fell 0.8% while the S&P 500 rose 0.6%, behind by 1.4 points.",
    "Most of the gap came from Energy.",
  ],
  missing: ["names: index artifact missing"],
  portfolio_return: -0.008,
  lead_return: 0.006,
  gap: -0.014,
};

describe("MarketNoteView", () => {
  it("renders the sentences and what was left out", () => {
    render(<MarketNoteView note={NOTE} />);
    expect(screen.getByText(/behind by 1.4 points\. Most of the gap came from Energy\./)).toBeTruthy();
    expect(screen.getByText(/Not included: names: index artifact missing/)).toBeTruthy();
  });

  it("still shows, with the reason, when there is no note", () => {
    render(<MarketNoteView note={{ ...NOTE, available: false, sentences: [],
      missing: ["today's session is not in the portfolio's history yet"] }} />);
    expect(screen.getByText(/Portfolio vs market/)).toBeTruthy();
    expect(screen.getByText(/not in the portfolio's history yet/)).toBeTruthy();
  });

  it("says it could not load rather than vanishing", () => {
    render(<MarketNoteView note={null} />);
    expect(screen.getByText(/did not load in time/)).toBeTruthy();
  });
});
