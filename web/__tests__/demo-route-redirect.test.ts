// `/dash/demo` publicly answered `307 Location: https://localhost:3003/portfolios/...`
// — NextResponse.redirect() needs an absolute URL and the only origin a route handler
// has is `req.url`, which behind the Worker → nginx → :3003 chain is the INTERNAL
// address. These assert the relative-Location fix, so the regression can't come back
// by someone reaching for NextResponse.redirect(new URL(..., req.url)) again.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DEMO_COOKIE, REFERENCE_PORTFOLIO_ID } from "@/lib/demo";

const ORIGINAL = process.env.NEXT_PUBLIC_BASE_PATH;

beforeEach(() => {
  vi.resetModules();
  vi.doMock("@/lib/track", () => ({ track: vi.fn().mockResolvedValue(undefined) }));
});

afterEach(() => {
  process.env.NEXT_PUBLIC_BASE_PATH = ORIGINAL;
  vi.resetModules();
  vi.doUnmock("@/lib/track");
});

describe("GET /demo", () => {
  it("redirects relative to the app, never to an absolute internal origin", async () => {
    process.env.NEXT_PUBLIC_BASE_PATH = "/dash";
    const { GET } = await import("@/app/demo/route");
    const res = await GET();
    const location = res.headers.get("location");

    expect(res.status).toBe(307);
    expect(location).toBe(`/dash/portfolios/${REFERENCE_PORTFOLIO_ID}`);
    // The actual bug: an absolute Location carrying the internal bind address.
    expect(location).not.toMatch(/^https?:\/\//);
    expect(location).not.toContain("localhost");
  });

  it("sets the demo cookie", async () => {
    process.env.NEXT_PUBLIC_BASE_PATH = "/dash";
    const { GET } = await import("@/app/demo/route");
    const res = await GET();
    expect(res.cookies.get(DEMO_COOKIE)?.value).toBe("1");
  });

  it("omits the prefix for a root-mounted build", async () => {
    process.env.NEXT_PUBLIC_BASE_PATH = "";
    const { GET } = await import("@/app/demo/route");
    const res = await GET();
    expect(res.headers.get("location")).toBe(`/portfolios/${REFERENCE_PORTFOLIO_ID}`);
  });
});

describe("POST /demo (exit demo)", () => {
  it("uses 303 so the browser GETs /login instead of re-POSTing to it", async () => {
    process.env.NEXT_PUBLIC_BASE_PATH = "/dash";
    const { POST } = await import("@/app/demo/route");
    const res = POST();
    // 307 preserves the method — the old status POSTed to a page route, which 405s.
    expect(res.status).toBe(303);
    expect(res.headers.get("location")).toBe("/dash/login");
  });

  it("expires the demo cookie", async () => {
    process.env.NEXT_PUBLIC_BASE_PATH = "/dash";
    const { POST } = await import("@/app/demo/route");
    const res = POST();
    const cookie = res.cookies.get(DEMO_COOKIE);
    expect(cookie?.value).toBe("");
    expect(cookie?.maxAge).toBe(0);
  });
});
