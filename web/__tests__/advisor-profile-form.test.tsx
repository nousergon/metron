// AdvisorProfileForm — the pre-registration suitability wall (metron-ops#164,
// metron-ops#166), client side. Two behaviors that would regress silently:
// (1) the suitability inputs (Strategy / Risk tolerance / Time horizon) are HIDDEN
// pre-registration, and (2) stored suitability values pass through a save UNCHANGED
// (editing targets must never wipe or mutate them — the fields stay in the API
// contract, only the inputs are gone).

import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

const mocks = vi.hoisted(() => ({
  saveProfileAction: vi.fn(async () => ({ ok: true, message: "Saved." })),
}));

vi.mock("@/app/portfolios/[id]/intelligence/profile/actions", () => ({
  saveProfileAction: mocks.saveProfileAction,
}));

import { AdvisorProfileForm } from "@/components/advisor-profile-form";
import type { AdvisorProfile } from "@/lib/api";

// Stored values a pre-wall tenant may already have — must survive a targets edit.
const SUITABILITY = {
  strategy: "stored-strategy-value",
  risk_tolerance: "stored-risk-value",
  time_horizon: "stored-horizon-value",
} as const;

const initial: AdvisorProfile = {
  ...SUITABILITY,
  target_allocation: { us_equity: 0.6, international: 0.2 },
  overweight_sectors: ["Health Care"],
  avoid_sectors: ["Energy"],
  income_target: 5000,
  max_single_position: 0.1,
  rebalance_frequency: "annually",
};

describe("AdvisorProfileForm suitability wall", () => {
  it("renders no suitability inputs pre-registration", () => {
    render(<AdvisorProfileForm portfolioId="p1" initial={initial} />);
    expect(screen.queryByText("Strategy")).toBeNull();
    expect(screen.queryByText("Risk tolerance")).toBeNull();
    expect(screen.queryByText("Time horizon")).toBeNull();
    // No input anywhere carries a stored suitability value.
    for (const value of Object.values(SUITABILITY)) {
      expect(screen.queryByDisplayValue(value)).toBeNull();
    }
    // The mechanical-targets inputs are still there — don't over-excise.
    expect(screen.getByText("Max single position (%)")).toBeInTheDocument();
    expect(screen.getByText("US equity target (%)")).toBeInTheDocument();
    expect(screen.getByText("Rebalance frequency")).toBeInTheDocument();
  });

  it("passes stored suitability values through a save unchanged", async () => {
    render(<AdvisorProfileForm portfolioId="p1" initial={initial} />);
    fireEvent.change(screen.getByLabelText("Max single position (%)"), { target: { value: "15" } });
    fireEvent.click(screen.getByRole("button", { name: /save profile/i }));

    await waitFor(() => expect(mocks.saveProfileAction).toHaveBeenCalledTimes(1));
    const [portfolioId, payload] = mocks.saveProfileAction.mock.calls[0] as unknown as [string, AdvisorProfile];
    expect(portfolioId).toBe("p1");
    // Suitability: untouched pass-through of the stored values.
    expect(payload.strategy).toBe(SUITABILITY.strategy);
    expect(payload.risk_tolerance).toBe(SUITABILITY.risk_tolerance);
    expect(payload.time_horizon).toBe(SUITABILITY.time_horizon);
    // The target edit went through (15% → 0.15 fraction).
    expect(payload.max_single_position).toBeCloseTo(0.15);
  });
});
