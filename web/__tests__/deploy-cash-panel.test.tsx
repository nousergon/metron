// DeployCashPanel (metron-ops#300, Brian ruling 2026-09-14) — the Overview panel renders
// the SERVER's plan and nothing of its own: the lines in server order, the constraints that
// bound each one, the unallocated remainder with its reasons, the limit block, the verbatim
// disclaimer, and the link to the rating's measured track record (metron-ops#295: IC ~= 0 at
// 1-20 days, so the evidence must be one click away from the ordering).

import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

const mocks = vi.hoisted(() => ({
  fetchDeployCashAction: vi.fn(),
}));

vi.mock("@/app/portfolios/[id]/deploy-cash-action", () => ({
  fetchDeployCashAction: mocks.fetchDeployCashAction,
}));

import { DeployCashPanel } from "@/components/deploy-cash-panel";
import type { DeployCashPlan } from "@/lib/api";

const DISCLAIMER =
  "Ranked by technical attractiveness under your position and sector limits. " +
  "Describes recent price action; not investment advice.";

const PLAN: DeployCashPlan = {
  as_of: "2026-09-14",
  amount_usd: 10000,
  allocated_usd: 4030,
  unallocated_usd: 5970,
  unallocated_reasons: ["the position-weight limit was reached"],
  lines: [
    {
      ticker: "KO",
      usd: 1380,
      shares_est: 23,
      price: 60,
      price_as_of: "2026-09-13",
      technical_label: "Strong Buy",
      score: 0.8,
      reasons: ["Technical rating Strong Buy (score +0.80, intraday basis)"],
      constraints_hit: ["max_position_weight"],
    },
    {
      ticker: "NVDA",
      usd: 1300,
      shares_est: 26,
      price: 50,
      price_as_of: "2026-09-13",
      technical_label: "Buy",
      score: 0.4,
      reasons: ["Technical rating Buy (score +0.40, intraday basis)"],
      constraints_hit: ["max_position_weight", "max_sector_weight"],
    },
  ],
  portfolio_value: 3800,
  deployment_basis: 13800,
  base_currency: "USD",
  rating_as_of: "2026-09-14T14:55:00Z",
  rating_basis: "intraday",
  config: {
    max_position_weight: 0.1,
    max_sector_weight: 0.3,
    min_line_usd: 500,
    whole_shares_only: true,
    eligible_labels: ["Buy", "Strong Buy"],
  },
  champion: "tech_score_desc_v1",
  disclaimer: DISCLAIMER,
  skipped: [],
};

async function submit(amount = "10000") {
  fireEvent.change(screen.getByLabelText("Amount to deploy"), { target: { value: amount } });
  fireEvent.click(screen.getByRole("button", { name: /rank candidates/i }));
}

describe("DeployCashPanel", () => {
  it("renders the disclaimer verbatim and the track-record link before any plan exists", () => {
    render(<DeployCashPanel portfolioId="p1" />);
    expect(screen.getByText(new RegExp(DISCLAIMER.slice(0, 60)))).toBeTruthy();
    const link = screen.getByRole("link", { name: /track record/i });
    expect(link.getAttribute("href")).toBe("/portfolios/p1/diagnostics");
  });

  it("renders the server's lines in the server's order, with the constraints that bound them", async () => {
    mocks.fetchDeployCashAction.mockResolvedValue({ ok: true, plan: PLAN });
    render(<DeployCashPanel portfolioId="p1" />);
    await submit();
    await waitFor(() => expect(screen.getByText("KO")).toBeTruthy());
    const rows = screen.getAllByRole("row").slice(1); // skip the header row
    expect(rows.map((r) => r.querySelector("td")?.textContent)).toEqual(["KO", "NVDA"]);
    expect(screen.getByText(/limited by max position weight, max sector weight/)).toBeTruthy();
  });

  it("reports the unallocated remainder with its reasons — never forces it into a line", async () => {
    mocks.fetchDeployCashAction.mockResolvedValue({ ok: true, plan: PLAN });
    render(<DeployCashPanel portfolioId="p1" />);
    await submit();
    await waitFor(() => expect(screen.getByText(/left unplaced/)).toBeTruthy());
    expect(screen.getByText("the position-weight limit was reached")).toBeTruthy();
  });

  it("shows the limit block the plan was actually run under", async () => {
    mocks.fetchDeployCashAction.mockResolvedValue({ ok: true, plan: PLAN });
    render(<DeployCashPanel portfolioId="p1" />);
    await submit();
    await waitFor(() => expect(screen.getByText(/Limits applied/)).toBeTruthy());
    expect(screen.getByText(/max 10.0% per position/)).toBeTruthy();
    expect(screen.getByText(/max 30.0% per sector/)).toBeTruthy();
    expect(screen.getByText(/whole shares only/)).toBeTruthy();
  });

  it("surfaces a failed plan instead of rendering a stale one", async () => {
    mocks.fetchDeployCashAction.mockResolvedValue({ ok: false, message: "This view isn't available on your plan." });
    render(<DeployCashPanel portfolioId="p1" />);
    await submit();
    await waitFor(() => expect(screen.getByText("This view isn't available on your plan.")).toBeTruthy());
    expect(screen.queryByText("KO")).toBeNull();
  });

  it("says so when no candidate clears the limits, rather than rendering an empty table", async () => {
    mocks.fetchDeployCashAction.mockResolvedValue({
      ok: true,
      plan: { ...PLAN, lines: [], allocated_usd: 0, unallocated_usd: 10000 },
    });
    render(<DeployCashPanel portfolioId="p1" />);
    await submit();
    await waitFor(() => expect(screen.getByText(/No candidate cleared the limits/)).toBeTruthy());
  });
});
