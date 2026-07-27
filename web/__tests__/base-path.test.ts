// Regression cover for the /dash basePath cutover (metron#298): Next auto-prefixes
// basePath ONLY for <Link>/redirect()/next-image, so every hand-built URL has to add
// it explicitly. When it didn't, the magic-link callbackURL resolved to
// metron.nousergon.ai/ — the MARKETING site — and sign-in looked broken even though
// the session cookie was set correctly.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const ORIGINAL = process.env.NEXT_PUBLIC_BASE_PATH;

afterEach(() => {
  process.env.NEXT_PUBLIC_BASE_PATH = ORIGINAL;
  vi.resetModules();
  vi.unstubAllGlobals();
});

async function load(basePath: string | undefined) {
  vi.resetModules();
  if (basePath === undefined) delete process.env.NEXT_PUBLIC_BASE_PATH;
  else process.env.NEXT_PUBLIC_BASE_PATH = basePath;
  return import("@/lib/base-path");
}

describe("withBasePath — deployed /dash build", () => {
  it("prefixes an app-absolute path", async () => {
    const { withBasePath } = await load("/dash");
    expect(withBasePath("/login")).toBe("/dash/login");
    expect(withBasePath("/demo")).toBe("/dash/demo");
    expect(withBasePath("/portfolios/abc-123")).toBe("/dash/portfolios/abc-123");
  });

  it("maps the app root to the bare basePath, with no trailing slash", async () => {
    const { withBasePath } = await load("/dash");
    // "/dash/" would work but costs a redirect hop and isn't the canonical URL.
    expect(withBasePath("/")).toBe("/dash");
  });

  it("rejects a path that is not app-absolute", async () => {
    const { withBasePath } = await load("/dash");
    expect(() => withBasePath("login")).toThrow(/app-absolute/);
  });
});

describe("withBasePath — local dev (basePath unset)", () => {
  it("is a passthrough", async () => {
    const { withBasePath } = await load(undefined);
    expect(withBasePath("/login")).toBe("/login");
    expect(withBasePath("/")).toBe("/");
  });

  it("treats an empty string the same as unset", async () => {
    const { withBasePath } = await load("");
    expect(withBasePath("/login")).toBe("/login");
    expect(withBasePath("/")).toBe("/");
  });
});

describe("appUrl — the magic-link callbackURL", () => {
  beforeEach(() => {
    vi.stubGlobal("window", { location: { origin: "https://metron.nousergon.ai" } });
  });

  it("lands the post-verify redirect INSIDE the app, not on the marketing root", async () => {
    const { appUrl } = await load("/dash");
    // The exact regression: this used to be `${origin}/`, which is the marketing site.
    expect(appUrl("/")).toBe("https://metron.nousergon.ai/dash");
  });

  it("qualifies a deeper app path", async () => {
    const { appUrl } = await load("/dash");
    expect(appUrl("/login")).toBe("https://metron.nousergon.ai/dash/login");
  });

  it("still works for a root-mounted (local dev) build", async () => {
    const { appUrl } = await load(undefined);
    expect(appUrl("/")).toBe("https://metron.nousergon.ai/");
  });
});
