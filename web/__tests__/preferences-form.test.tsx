// PreferencesForm — suitability inputs retired pre-registration (metron-ops#174,
// ruled 2026-07-09; same class as the metron-ops#166 AdvisorProfile wall). Two
// behaviors that would regress silently: (1) the Risk tolerance / Objective inputs
// are GONE while unregistered, and (2) stored values pass through a save UNCHANGED
// (editing notes must never wipe them — the fields stay in the API contract, only
// the inputs are gone).

import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

const mocks = vi.hoisted(() => ({
  savePreferencesAction: vi.fn(async () => ({ ok: true, message: "Saved." })),
  restoreExcludedAccountAction: vi.fn(async () => ({ ok: true, message: "ok" })),
  updateAccountTagsAction: vi.fn(async () => ({ ok: true, message: "ok" })),
  updateBaseCurrencyAction: vi.fn(async () => ({ ok: true, message: "ok" })),
}));

vi.mock("@/app/portfolios/[id]/actions", () => ({
  savePreferencesAction: mocks.savePreferencesAction,
  restoreExcludedAccountAction: mocks.restoreExcludedAccountAction,
  updateAccountTagsAction: mocks.updateAccountTagsAction,
  updateBaseCurrencyAction: mocks.updateBaseCurrencyAction,
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn() }),
}));

import { PreferencesForm } from "@/components/settings-forms";
import type { Preferences } from "@/lib/api";

// Stored values a pre-retirement tenant may already have — must survive a notes edit.
const current: Preferences = {
  risk_tolerance: "aggressive",
  objective: "growth",
  notes: "existing notes",
  intraday_enabled: false,
};

describe("PreferencesForm suitability retirement", () => {
  it("renders no risk-tolerance or objective inputs pre-registration", () => {
    render(<PreferencesForm portfolioId="p1" current={current} />);
    expect(screen.queryByText("Risk tolerance")).toBeNull();
    expect(screen.queryByText("Objective")).toBeNull();
    expect(document.querySelector("select")).toBeNull();
  });

  it("passes stored suitability values through a save unchanged", async () => {
    render(<PreferencesForm portfolioId="p1" current={current} />);
    fireEvent.change(screen.getByDisplayValue("existing notes"), {
      target: { value: "updated notes" },
    });
    fireEvent.click(screen.getByRole("button", { name: /save/i }));
    await waitFor(() => expect(mocks.savePreferencesAction).toHaveBeenCalledTimes(1));
    expect(mocks.savePreferencesAction).toHaveBeenCalledWith("p1", {
      risk_tolerance: "aggressive",
      objective: "growth",
      notes: "updated notes",
      intraday_enabled: false,
    });
  });
});
