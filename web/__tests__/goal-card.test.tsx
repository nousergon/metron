// GoalCard — the Overview surface (metron-ops-I316). Empty state links to Settings
// rather than showing a number; a set goal renders progress/trajectory/drag without
// ever crossing into advice-flavored copy.

import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import { GoalCard } from "@/components/goal-card";
import type { GoalFacets } from "@/lib/api-goal";

const UNAVAILABLE: GoalFacets = {
  goal_progress: { available: false, as_of: "2026-09-15", reason: "no_goal_set" },
  goal_trajectory_range: { available: false, as_of: "2026-09-15", reason: "no_goal_set" },
  goal_timing_cost: { available: false, as_of: "2026-09-15", reason: "no_goal_set" },
  goal_fee_drag: { available: false, as_of: "2026-09-15", reason: "expense_ratio_source_not_provisioned" },
  goal_asset_location_drag: { available: false, as_of: "2026-09-15", reason: "no_accounts" },
  goal_withdrawal_readiness: { available: false, as_of: "2026-09-15", reason: "no_withdrawal_rate_set" },
};

const AVAILABLE: GoalFacets = {
  goal_progress: {
    available: true,
    as_of: "2026-09-15",
    current_value_usd: 250_000,
    target_amount_usd: 1_000_000,
    progress_ratio: 0.25,
    period_change_ratio: 0.02,
  },
  goal_trajectory_range: {
    available: true,
    as_of: "2026-09-15",
    years_low: 12,
    years_high: 18,
    by_window: { "1y": 12, "3y": 15, since_inception: 18 },
    rates: { "1y": 0.09, "3y": 0.07, since_inception: 0.05 },
  },
  goal_timing_cost: { available: true, as_of: "2026-09-15", gap_pct: -0.02, years_added: 1.2 },
  goal_fee_drag: { available: false, as_of: "2026-09-15", reason: "expense_ratio_source_not_provisioned" },
  goal_asset_location_drag: {
    available: true,
    as_of: "2026-09-15",
    trailing_days: 365,
    taxable_dividend_interest_income_usd: 1200,
    sheltered_dividend_interest_income_usd: 300,
    assumed_tax_rate: null,
    estimated_tax_usd_per_year: null,
  },
  goal_withdrawal_readiness: {
    available: true,
    as_of: "2026-09-15",
    withdrawal_rate: 0.04,
    annual_withdrawal_usd: 40_000,
    monthly_withdrawal_usd: 3_333.33,
    by_account_type: { taxable: { available_usd: 20_000, months_covered: 6 } },
  },
};

describe("GoalCard empty state", () => {
  it("links to Settings and shows no number when no goal is set", () => {
    render(<GoalCard portfolioId="p1" facets={UNAVAILABLE} />);
    expect(screen.getByText(/no goal set yet/i)).toBeInTheDocument();
    const link = screen.getByRole("link", { name: /set your retirement number/i });
    expect(link).toHaveAttribute("href", "/portfolios/p1/settings");
  });
});

describe("GoalCard with a set goal", () => {
  it("renders progress, trajectory, and drag facets", () => {
    render(<GoalCard portfolioId="p1" facets={AVAILABLE} />);
    expect(screen.getByText("25.0%")).toBeInTheDocument();
    expect(screen.getByText(/12–18y/)).toBeInTheDocument();
    expect(screen.getByText(/\+1\.2y/)).toBeInTheDocument();
  });

  it("never renders 'should', 'recommend', or a promissory 'on track to'", () => {
    const { container } = render(<GoalCard portfolioId="p1" facets={AVAILABLE} />);
    const text = container.textContent?.toLowerCase() ?? "";
    expect(text).not.toMatch(/\bshould\b/);
    expect(text).not.toMatch(/\brecommend/);
    expect(text).not.toMatch(/\bon track to\b/);
  });
});
