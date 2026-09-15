// GoalForm — the retirement-goal inputs (metron-ops-I316). Two behaviors that must
// never regress: (1) the empty state renders NO pre-filled values (no defaults, no
// suggested target), and (2) a save sends exactly what the user typed, percent
// converted to a fraction. A third test renders at phone width (390px).

import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

const mocks = vi.hoisted(() => ({
  saveGoalAction: vi.fn(async () => ({ ok: true, message: "Goal saved." })),
}));

vi.mock("@/app/portfolios/[id]/goal-actions", () => ({
  saveGoalAction: mocks.saveGoalAction,
}));

import { GoalForm } from "@/components/goal-form";
import { EMPTY_GOAL, type Goal } from "@/lib/api-goal";

describe("GoalForm empty state", () => {
  it("renders every input blank when no goal is saved — no pre-fill", () => {
    const { container } = render(<GoalForm portfolioId="p1" current={EMPTY_GOAL} />);
    expect(screen.getByPlaceholderText("e.g. 1500000")).toHaveValue(null);
    expect(screen.getByPlaceholderText("e.g. 25000")).toHaveValue(null);
    expect(screen.getByPlaceholderText("e.g. 4")).toHaveValue(null);
    const dateInput = container.querySelector('input[type="date"]') as HTMLInputElement;
    expect(dateInput.value).toBe("");
  });

  it("saves typed values, converting the withdrawal-rate percent to a fraction", async () => {
    render(<GoalForm portfolioId="p1" current={EMPTY_GOAL} />);
    fireEvent.change(screen.getByPlaceholderText("e.g. 1500000"), { target: { value: "1000000" } });
    fireEvent.change(screen.getByPlaceholderText("e.g. 25000"), { target: { value: "20000" } });
    fireEvent.change(screen.getByPlaceholderText("e.g. 4"), { target: { value: "4" } });
    fireEvent.click(screen.getByRole("button", { name: /save goal/i }));

    await waitFor(() => expect(mocks.saveGoalAction).toHaveBeenCalledTimes(1));
    const [portfolioId, goal] = mocks.saveGoalAction.mock.calls[0] as unknown as [string, Goal];
    expect(portfolioId).toBe("p1");
    expect(goal.target_amount_usd).toBe(1_000_000);
    expect(goal.annual_contribution_usd).toBe(20_000);
    expect(goal.withdrawal_rate).toBeCloseTo(0.04);
  });

  it("round-trips a saved goal back into the inputs (fraction to percent)", () => {
    const current: Goal = {
      target_amount_usd: 750_000,
      target_date: "2040-06-01",
      annual_contribution_usd: 12_000,
      withdrawal_rate: 0.035,
    };
    render(<GoalForm portfolioId="p1" current={current} />);
    expect(screen.getByPlaceholderText("e.g. 1500000")).toHaveValue(750000);
    expect(screen.getByPlaceholderText("e.g. 25000")).toHaveValue(12000);
    expect(screen.getByPlaceholderText("e.g. 4")).toHaveValue(3.5);
  });

  it("renders at phone width (390px) without error", () => {
    const original = window.innerWidth;
    Object.defineProperty(window, "innerWidth", { writable: true, configurable: true, value: 390 });
    window.dispatchEvent(new Event("resize"));
    render(<GoalForm portfolioId="p1" current={EMPTY_GOAL} />);
    expect(screen.getByRole("button", { name: /save goal/i })).toBeInTheDocument();
    Object.defineProperty(window, "innerWidth", { writable: true, configurable: true, value: original });
  });
});

// ── copy lint: banned advice-flavored phrasing ───────────────────────────────

describe("GoalForm copy lint", () => {
  it("never renders 'should', 'recommend', or a promissory 'on track to'", () => {
    const { container } = render(<GoalForm portfolioId="p1" current={EMPTY_GOAL} />);
    const text = container.textContent?.toLowerCase() ?? "";
    expect(text).not.toMatch(/\bshould\b/);
    expect(text).not.toMatch(/\brecommend/);
    expect(text).not.toMatch(/\bon track to\b/);
  });
});
