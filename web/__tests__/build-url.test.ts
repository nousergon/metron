// buildUrl (config#2611, CodeQL js/request-forgery sweep): every backend fetch in
// lib/api.ts resolves its target through this one chokepoint instead of blind
// `${API_URL}${path}` string concatenation, so a path segment can never smuggle a
// different scheme/host into the request — `new URL(path, API_URL)` gives a real URL
// parser the final say, and the origin assertion is the backstop if it ever tried.
import { afterEach, describe, expect, it, vi } from "vitest";

const ORIGINAL_ENV = process.env.METRON_API_URL;

describe("buildUrl", () => {
  afterEach(() => {
    process.env.METRON_API_URL = ORIGINAL_ENV;
    vi.resetModules();
  });

  it("resolves a normal path against the configured API origin", async () => {
    vi.resetModules();
    process.env.METRON_API_URL = "https://api.metron.example";
    const { buildUrl } = await import("@/lib/api");
    expect(buildUrl("/portfolios/abc-123/holdings?valuation=live")).toBe(
      "https://api.metron.example/portfolios/abc-123/holdings?valuation=live",
    );
  });

  it("refuses a path that would resolve to a different host via protocol-relative //", async () => {
    vi.resetModules();
    process.env.METRON_API_URL = "https://api.metron.example";
    const { buildUrl } = await import("@/lib/api");
    expect(() => buildUrl("//evil.example/steal")).toThrow(/cross-origin/);
  });

  it("refuses a path carrying its own absolute http(s) origin", async () => {
    vi.resetModules();
    process.env.METRON_API_URL = "https://api.metron.example";
    const { buildUrl } = await import("@/lib/api");
    expect(() => buildUrl("https://evil.example/portfolios/1")).toThrow(/cross-origin/);
  });

  it("stays on the configured origin even when a path segment looks like a host", async () => {
    vi.resetModules();
    process.env.METRON_API_URL = "https://api.metron.example";
    const { buildUrl } = await import("@/lib/api");
    // A raw (unencoded) id containing "@evil.example" is just a path segment here —
    // new URL(path, base) resolution never lets a relative path rewrite the authority.
    expect(buildUrl("/portfolios/@evil.example/holdings")).toBe(
      "https://api.metron.example/portfolios/@evil.example/holdings",
    );
  });
});
