// The post-auth landing route: a signed-in tenant with exactly one portfolio lands on its
// Holdings page (Brian, 2026-11-04; previously the glance screen, metron-ops-I328). A
// tenant with no connected accounts keeps the create-portfolio empty state; a tenant with
// more than one portfolio keeps the picker list.
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
  it("redirects a tenant with a populated portfolio straight to its Holdings page", async () => {
    mockGetPortfolios.mockResolvedValue([{ id: "port-1", name: "Main", base_currency: "USD" }]);
    await expect(HomePage()).rejects.toThrow(REDIRECT);
    expect(mockRedirect).toHaveBeenCalledWith("/portfolios/port-1");
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
