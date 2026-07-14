// requireApiAuth's fallback ladder (metron-ops#183): the demo credential is ONLY for
// visitors with no session cookie at all. A request that carries a session cookie but
// fails the mint must land on /login — never silently on the demo tenant's data.
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

const REDIRECT = new Error("NEXT_REDIRECT_SENTINEL");
const mockHeaders = vi.fn();
const mockCookies = vi.fn();
const mockRedirect = vi.fn((_url: string) => {
  throw REDIRECT;
});

vi.mock("next/headers", () => ({
  headers: () => mockHeaders(),
  cookies: () => mockCookies(),
}));
vi.mock("next/navigation", () => ({ redirect: (url: string) => mockRedirect(url) }));
// react's cache() memoizes per render tree — identity passthrough keeps each test call fresh.
vi.mock("react", async (orig) => ({
  ...(await orig<typeof import("react")>()),
  cache: <T,>(fn: T) => fn,
}));

const SESSION_COOKIE = "__Secure-better-auth.session_token";

function arrange(opts: { cookieHeader: string | null; jar: Record<string, string> }) {
  mockHeaders.mockReturnValue({ get: (k: string) => (k === "cookie" ? opts.cookieHeader : null) });
  mockCookies.mockReturnValue({
    get: (k: string) => (k in opts.jar ? { name: k, value: opts.jar[k] } : undefined),
  });
}

function mockMint(status: number, token?: string) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok: status === 200,
      status,
      json: async () => ({ token }),
    } as Response),
  );
}

beforeEach(() => {
  vi.clearAllMocks();
});
afterEach(() => {
  vi.unstubAllGlobals();
});

async function requireApiAuth() {
  const mod = await import("@/lib/session");
  return mod.requireApiAuth();
}

describe("requireApiAuth fallback ladder", () => {
  it("returns the minted JWT for a valid session", async () => {
    arrange({ cookieHeader: `${SESSION_COOKIE}=abc.sig`, jar: { [SESSION_COOKIE]: "abc.sig" } });
    mockMint(200, "jwt-123");
    await expect(requireApiAuth()).resolves.toBe("jwt-123");
  });

  it("redirects to /login — NOT demo — when a session cookie is present but the mint 401s, even with the demo cookie set", async () => {
    arrange({
      cookieHeader: `${SESSION_COOKIE}=stale.sig; metron-demo=1`,
      jar: { [SESSION_COOKIE]: "stale.sig", "metron-demo": "1" },
    });
    mockMint(401);
    await expect(requireApiAuth()).rejects.toThrow(REDIRECT);
    expect(mockRedirect).toHaveBeenCalledWith("/login");
  });

  it("falls back to the demo credential only when no session cookie exists", async () => {
    arrange({ cookieHeader: "metron-demo=1", jar: { "metron-demo": "1" } });
    mockMint(401); // must not even be consulted for the decision
    const { DEMO_TENANT_ID } = await import("@/lib/demo");
    await expect(requireApiAuth()).resolves.toBe(DEMO_TENANT_ID);
  });

  it("redirects to /login for a cookie-less, demo-less visitor", async () => {
    arrange({ cookieHeader: null, jar: {} });
    await expect(requireApiAuth()).rejects.toThrow(REDIRECT);
    expect(mockRedirect).toHaveBeenCalledWith("/login");
  });

  it("fails loud (throws, no redirect) when the auth service errors", async () => {
    arrange({ cookieHeader: `${SESSION_COOKIE}=abc.sig`, jar: { [SESSION_COOKIE]: "abc.sig" } });
    mockMint(503);
    await expect(requireApiAuth()).rejects.toThrow("auth token mint → 503");
    expect(mockRedirect).not.toHaveBeenCalled();
  });
});
