// PlanTargetsForm (metron-ops-I311) — the no-default-value invariant (intelligence-
// doctrine layer 2: "Metron does not choose securities" — a pre-filled ticker or weight
// would be Metron choosing) and a phone-width (390px) render smoke test.

import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";

const mocks = vi.hoisted(() => ({
  savePlanTargetsAction: vi.fn(),
}));

vi.mock("@/app/portfolios/[id]/planning-actions", () => ({
  savePlanTargetsAction: mocks.savePlanTargetsAction,
}));

import { PlanTargetsForm } from "@/components/plan-targets-form";
import type { PlanTargets } from "@/lib/api-planning";

const EMPTY: PlanTargets = {
  targets: [],
  max_single_position: null,
  min_line_usd: null,
  available: true,
  reason: null,
  required_tier: null,
};

afterEach(cleanup);

describe("PlanTargetsForm — no default/suggested values", () => {
  it("renders every input empty when nothing has been saved yet", () => {
    render(<PlanTargetsForm portfolioId="p1" initial={EMPTY} />);
    const tickerInput = screen.getByLabelText("Target 1 ticker") as HTMLInputElement;
    const weightInput = screen.getByLabelText("Target 1 weight percent") as HTMLInputElement;
    const capInput = screen.getByLabelText("Max per position percent") as HTMLInputElement;
    const minLineInput = screen.getByLabelText("Minimum line size in dollars") as HTMLInputElement;
    expect(tickerInput.value).toBe("");
    expect(weightInput.value).toBe("");
    expect(capInput.value).toBe("");
    expect(minLineInput.value).toBe("");
  });

  it("echoes back exactly what was already saved — never a value the user didn't type", () => {
    const saved: PlanTargets = {
      targets: [{ symbol: "AAPL", weight: 0.1 }],
      max_single_position: 0.25,
      min_line_usd: 100,
      available: true,
      reason: null,
      required_tier: null,
    };
    render(<PlanTargetsForm portfolioId="p1" initial={saved} />);
    expect((screen.getByLabelText("Target 1 ticker") as HTMLInputElement).value).toBe("AAPL");
    expect((screen.getByLabelText("Target 1 weight percent") as HTMLInputElement).value).toBe("10");
  });
});

describe("PlanTargetsForm — phone width (390px)", () => {
  it("renders without error at a 390px viewport", () => {
    const original = window.innerWidth;
    Object.defineProperty(window, "innerWidth", { writable: true, configurable: true, value: 390 });
    window.dispatchEvent(new Event("resize"));
    try {
      render(<PlanTargetsForm portfolioId="p1" initial={EMPTY} />);
      expect(screen.getByRole("button", { name: /save targets/i })).toBeTruthy();
      expect(screen.getByRole("button", { name: /add a target/i })).toBeTruthy();
    } finally {
      Object.defineProperty(window, "innerWidth", { writable: true, configurable: true, value: original });
    }
  });
});
