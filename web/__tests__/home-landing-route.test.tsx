// The post-auth landing route (metron-ops#248 / metron-ops-I328, Brian ruling 2026-07-30):
// a signed-in tenant with exactly one portfolio lands on its glance screen, not the old
// Holdings/picker route — the last unmet clause of the I248 epic. Covers both landing
// cases named in the issue: a tenant with a populated portfolio (redirects into glance)
// and a tenant with no connected accounts yet (keeps the create-portfolio empty state,
// since there is nothing to glance at). A tenant holding more than one portfolio keeps
// the existing picker list — the glance screen has no multi-portfolio aggregate.
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";

const REDIRECT = new Error("NEXT_REDIRECT_SENTINEL");
const mockRedirect = vi.fn((_url: string) => {
  throw REDIRECT;
});
const mockRequireApiAuth = vi.fn();
const mockGetPortfolios = vi.fn();

vi.mock("next/navigation", () => ({ redirect: (url: string) => mockRedirect(url) }));
vi.mock("@/lib/session", () => ({ requireApiAuth: () => mockRequireApiAuth() }));
vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  getPortfolios: (auth: string) => mockGetPortfolios(auth),
}));

beforeEach(() => {
  vi.clearAllMocks();
  mockRequireApiAuth.mockResolvedValue("jwt-123");
});
afterEach(() => {
  vi.resetModules();
});

async function HomePage() {
  const mod = await import("@/app/page");
  return mod.default();
}

describe("post-auth landing route (/)", () => {
  it("redirects a tenant with a populated portfolio straight to its glance screen", async () => {
    mockGetPortfolios.mockResolvedValue([{ id: "port-1", name: "Main", base_currency: "USD" }]);
    await expect(HomePage()).rejects.toThrow(REDIRECT);
    expect(mockRedirect).toHaveBeenCalledWith("/portfolios/port-1/glance");
  });

  it("renders the empty create-portfolio state for a tenant with no connected accounts, never a redirect", async () => {
    mockGetPortfolios.mockResolvedValue([]);
    const el = await HomePage();
    expect(mockRedirect).not.toHaveBeenCalled();
    render(el);
    expect(screen.getByText(/No portfolios yet/)).toBeInTheDocument();
  });

  it("keeps the picker list — not a redirect — for a tenant holding more than one portfolio", async () => {
    mockGetPortfolios.mockResolvedValue([
      { id: "port-1", name: "Main", base_currency: "USD" },
      { id: "port-2", name: "Side", base_currency: "USD" },
    ]);
    const el = await HomePage();
    expect(mockRedirect).not.toHaveBeenCalled();
    render(el);
    expect(screen.getByText("Main")).toBeInTheDocument();
    expect(screen.getByText("Side")).toBeInTheDocument();
  });
});
