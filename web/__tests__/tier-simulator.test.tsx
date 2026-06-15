// TierSimulator — the owner-only preview control: hidden unless simulator=true,
// reflects the active tier/feed, and persists changes via cookie + router.refresh().

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

const mocks = vi.hoisted(() => ({ refresh: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: mocks.refresh }) }));

import { TierSimulator } from "@/components/tier-simulator";
import type { Entitlements } from "@/lib/api";

function ent(overrides: Partial<Entitlements> = {}): Entitlements {
  return {
    tier: "personal",
    feed_enabled: true,
    provisioned_sources: [],
    features: [],
    tiers: [
      { key: "beta", label: "Beta (free)" },
      { key: "pro", label: "Pro" },
      { key: "agentic", label: "Research / Pro+" },
      { key: "personal", label: "Base (personal)" },
    ],
    simulator: true,
    ...overrides,
  };
}

beforeEach(() => {
  mocks.refresh.mockClear();
  document.cookie = "metron_preview_tier=; max-age=0; path=/";
  document.cookie = "metron_preview_feed=; max-age=0; path=/";
});

describe("TierSimulator", () => {
  it("renders nothing when the simulator is off (public product)", () => {
    const { container } = render(<TierSimulator entitlements={ent({ simulator: false })} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("reflects the active tier + feed state", () => {
    render(<TierSimulator entitlements={ent({ tier: "pro", feed_enabled: false })} />);
    expect((screen.getByLabelText("Preview tier") as HTMLSelectElement).value).toBe("pro");
    expect((screen.getByLabelText("Market-data feed") as HTMLInputElement).checked).toBe(false);
  });

  it("sets the preview cookie + refreshes on tier change", () => {
    render(<TierSimulator entitlements={ent()} />);
    fireEvent.change(screen.getByLabelText("Preview tier"), { target: { value: "beta" } });
    expect(document.cookie).toContain("metron_preview_tier=beta");
    expect(mocks.refresh).toHaveBeenCalled();
  });

  it("sets the feed cookie + refreshes on feed toggle", () => {
    render(<TierSimulator entitlements={ent({ feed_enabled: true })} />);
    fireEvent.click(screen.getByLabelText("Market-data feed"));
    expect(document.cookie).toContain("metron_preview_feed=false");
    expect(mocks.refresh).toHaveBeenCalled();
  });
});
