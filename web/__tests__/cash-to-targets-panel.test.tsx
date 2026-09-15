// CashToTargetsPanel (metron-ops-I311) — renders the server's plan verbatim, in the
// server's row order, never widening beyond the returned lines; the empty-targets state
// links back to the editor instead of running; phone-width (390px) smoke test.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

const mocks = vi.hoisted(() => ({
  fetchCashToTargetsAction: vi.fn(),
}));

vi.mock("@/app/portfolios/[id]/planning-actions", () => ({
  fetchCashToTargetsAction: mocks.fetchCashToTargetsAction,
}));

import { CashToTargetsPanel } from "@/components/cash-to-targets-panel";
import type { CashToTargetsPlan } from "@/lib/api-planning";

const PLAN: CashToTargetsPlan = {
  as_of: "2026-09-15",
  amount_usd: 1000,
  allocated_usd: 900,
  unallocated_usd: 100,
  unallocated_reasons: ["the remainder is smaller than the minimum line size"],
  lines: [
    { symbol: "KO", target_weight: 0.5, weight_before: 0.0, weight_after: 0.45, shares: 15, price: 60, usd: 900, price_as_of: "2026-09-13" },
  ],
  portfolio_value: 1000,
  deployment_basis: 2000,
  base_currency: "USD",
  max_single_position: 0.5,
  min_line_usd: 50,
  disclaimer: "Arithmetic against the targets you set. Metron does not choose securities.",
  available: true,
  reason: null,
  required_tier: null,
};

describe("CashToTargetsPanel", () => {
  it("shows the empty state and never renders a form when no targets are saved", () => {
    render(<CashToTargetsPanel portfolioId="p1" hasTargets={false} />);
    expect(screen.getByText(/Set your targets above first/)).toBeTruthy();
    expect(screen.queryByLabelText("Amount to deploy against your targets")).toBeNull();
  });

  it("renders the server's lines in the server's order", async () => {
    mocks.fetchCashToTargetsAction.mockResolvedValue({ ok: true, plan: PLAN });
    render(<CashToTargetsPanel portfolioId="p1" hasTargets />);
    fireEvent.change(screen.getByLabelText("Amount to deploy against your targets"), { target: { value: "1000" } });
    fireEvent.click(screen.getByRole("button", { name: /compute purchases/i }));
    await waitFor(() => expect(screen.getByText("KO")).toBeTruthy());
    expect(screen.getByText(/left unplaced/)).toBeTruthy();
  });

  it("renders the verbatim disclaimer", () => {
    render(<CashToTargetsPanel portfolioId="p1" hasTargets />);
    expect(screen.getByText("Arithmetic against the targets you set. Metron does not choose securities.")).toBeTruthy();
  });

  it("renders without error at a 390px viewport", () => {
    const original = window.innerWidth;
    Object.defineProperty(window, "innerWidth", { writable: true, configurable: true, value: 390 });
    try {
      render(<CashToTargetsPanel portfolioId="p1" hasTargets />);
      expect(screen.getByRole("button", { name: /compute purchases/i })).toBeTruthy();
    } finally {
      Object.defineProperty(window, "innerWidth", { writable: true, configurable: true, value: original });
    }
  });
});
