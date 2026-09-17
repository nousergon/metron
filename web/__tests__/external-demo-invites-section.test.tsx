// Owner-only "External demo invites" Settings section (metron-ops-I323). Behaviors that
// would regress silently: (1) "Create invite" is disabled while the demo is unreleased,
// with the reason shown, (2) a created code is shown exactly once, with the full
// paste-ready URL, and (3) the counters/invites tables render "not measured" — never a
// silent zero or empty list — when their data couldn't be loaded.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

const mocks = vi.hoisted(() => ({
  createExternalDemoInviteAction: vi.fn(async () => ({
    ok: true as const,
    code: "the-plaintext-code",
    expiresAt: "2026-10-01T00:00:00Z",
  })),
  revokeExternalDemoInviteAction: vi.fn(async () => ({ ok: true, message: "Invite revoked." })),
}));

vi.mock("@/app/portfolios/[id]/external-demo-invites-actions", () => ({
  createExternalDemoInviteAction: mocks.createExternalDemoInviteAction,
  revokeExternalDemoInviteAction: mocks.revokeExternalDemoInviteAction,
}));

import { ExternalDemoInvitesSection } from "@/components/external-demo-invites-section";
import type { ExternalDemoCounters, ExternalDemoInvite } from "@/lib/api";

const RELEASED_COUNTERS: ExternalDemoCounters = {
  invites_created: 2,
  sessions_started: 1,
  locked_card_taps: { deploy_cash: 3, market_board: 0 },
  external_demo_released: true,
};

const UNRELEASED_COUNTERS: ExternalDemoCounters = { ...RELEASED_COUNTERS, external_demo_released: false };

const INVITES: ExternalDemoInvite[] = [
  {
    id: "11111111-1111-1111-1111-111111111111",
    created_at: "2026-09-01T00:00:00Z",
    expires_at: "2026-09-15T00:00:00Z",
    redeemed_at: null,
    live_session_count: 0,
  },
];

describe("ExternalDemoInvitesSection — release gate", () => {
  it("disables Create invite and explains why while unreleased", () => {
    render(<ExternalDemoInvitesSection portfolioId="p1" counters={UNRELEASED_COUNTERS} invites={[]} />);
    expect(screen.getByRole("button", { name: /create invite/i })).toBeDisabled();
    expect(screen.getByText(/external demo not released/i)).toBeInTheDocument();
    expect(screen.getByText(/metron-ops#24/)).toBeInTheDocument();
  });

  it("enables Create invite once released, with no explanation shown", () => {
    render(<ExternalDemoInvitesSection portfolioId="p1" counters={RELEASED_COUNTERS} invites={[]} />);
    expect(screen.getByRole("button", { name: /create invite/i })).toBeEnabled();
    expect(screen.queryByText(/external demo not released/i)).toBeNull();
  });
});

describe("ExternalDemoInvitesSection — one-time code display", () => {
  it("shows the plaintext code once, as a full URL, after creating", async () => {
    render(<ExternalDemoInvitesSection portfolioId="p1" counters={RELEASED_COUNTERS} invites={[]} />);
    fireEvent.click(screen.getByRole("button", { name: /create invite/i }));
    await waitFor(() => expect(mocks.createExternalDemoInviteAction).toHaveBeenCalledWith("p1"));
    expect(await screen.findByText(/shown once/i)).toBeInTheDocument();
    expect(screen.getByText(/\/invite\?code=the-plaintext-code/)).toBeInTheDocument();
  });
});

describe("ExternalDemoInvitesSection — not-measured rendering", () => {
  it("renders invites as not-measured (never an empty list) when the list failed to load", () => {
    render(<ExternalDemoInvitesSection portfolioId="p1" counters={RELEASED_COUNTERS} invites={null} />);
    expect(screen.getByText(/not measured/i)).toBeInTheDocument();
    expect(screen.queryByText(/no invites yet/i)).toBeNull();
  });

  it("renders the real invites list when it loaded", () => {
    render(<ExternalDemoInvitesSection portfolioId="p1" counters={RELEASED_COUNTERS} invites={INVITES} />);
    expect(screen.getByRole("button", { name: /revoke/i })).toBeInTheDocument();
    expect(screen.queryByText(/not measured/i)).toBeNull();
  });

  it("renders real counters, including a true zero, distinctly from not-measured", () => {
    render(<ExternalDemoInvitesSection portfolioId="p1" counters={RELEASED_COUNTERS} invites={[]} />);
    expect(screen.getByText("2")).toBeInTheDocument(); // invites_created
    expect(screen.getByText("1")).toBeInTheDocument(); // sessions_started
    expect(screen.getByText("0")).toBeInTheDocument(); // market_board taps — a real zero
  });
});

describe("ExternalDemoInvitesSection — revoke", () => {
  it("calls the revoke action with the invite id", async () => {
    render(<ExternalDemoInvitesSection portfolioId="p1" counters={RELEASED_COUNTERS} invites={INVITES} />);
    fireEvent.click(screen.getByRole("button", { name: /revoke/i }));
    await waitFor(() =>
      expect(mocks.revokeExternalDemoInviteAction).toHaveBeenCalledWith("p1", INVITES[0].id),
    );
  });
});
