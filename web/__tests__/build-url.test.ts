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
    // Caught by the path-shape guard (leading `//`) before the origin check even runs.
    expect(() => buildUrl("//evil.example/steal")).toThrow(/unsafe path shape/);
  });

  it("refuses a path carrying its own absolute http(s) origin", async () => {
    vi.resetModules();
    process.env.METRON_API_URL = "https://api.metron.example";
    const { buildUrl } = await import("@/lib/api");
    // Caught by the path-shape guard (no leading `/`, and `:` is never in the allowed
    // charset) before the origin check even runs.
    expect(() => buildUrl("https://evil.example/portfolios/1")).toThrow(/unsafe path shape/);
  });

  it("refuses a path carrying an embedded scheme-like colon mid-segment", async () => {
    vi.resetModules();
    process.env.METRON_API_URL = "https://api.metron.example";
    const { buildUrl } = await import("@/lib/api");
    // `new URL` would resolve this harmlessly as a literal path segment (no bypass), but
    // the shape guard rejects any `:` outright — a stricter, easier-to-verify barrier
    // than relying on `new URL`'s relative-resolution semantics for every future path.
    expect(() => buildUrl("/portfolios/evil:example.com/data")).toThrow(/unsafe path shape/);
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

// apiFetch is the actual request chokepoint every call site in lib/api.ts uses (the
// origin check is re-inlined here rather than delegated to buildUrl — see that
// function's docstring) — covers the same cases directly against the real fetch sink.
describe("apiFetch", () => {
  afterEach(() => {
    process.env.METRON_API_URL = ORIGINAL_ENV;
    vi.resetModules();
    vi.unstubAllGlobals();
  });

  it("issues the request against the configured origin", async () => {
    vi.resetModules();
    process.env.METRON_API_URL = "https://api.metron.example";
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const { apiFetch } = await import("@/lib/api");
    await apiFetch("/portfolios/abc-123");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [calledUrl] = fetchMock.mock.calls[0];
    expect((calledUrl as URL).toString()).toBe("https://api.metron.example/portfolios/abc-123");
  });

  it("refuses to call fetch for a protocol-relative path", async () => {
    vi.resetModules();
    process.env.METRON_API_URL = "https://api.metron.example";
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const { apiFetch } = await import("@/lib/api");
    await expect(apiFetch("//evil.example/steal")).rejects.toThrow(/unsafe path shape/);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("refuses to call fetch for a path carrying its own absolute origin", async () => {
    vi.resetModules();
    process.env.METRON_API_URL = "https://api.metron.example";
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const { apiFetch } = await import("@/lib/api");
    await expect(apiFetch("https://evil.example/portfolios/1")).rejects.toThrow(/unsafe path shape/);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("refuses to call fetch for a path carrying an embedded scheme-like colon mid-segment", async () => {
    vi.resetModules();
    process.env.METRON_API_URL = "https://api.metron.example";
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const { apiFetch } = await import("@/lib/api");
    await expect(apiFetch("/portfolios/evil:example.com/data")).rejects.toThrow(/unsafe path shape/);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
