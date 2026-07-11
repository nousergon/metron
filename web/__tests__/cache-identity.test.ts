// cacheIdentity (metron-ops#179): unstable_cache keyParts/tags must key on the STABLE
// identity behind the API credential — the demo tenant id as-is, or the JWT `sub`
// claim — never the short-lived token string, which rotates every mint and would turn
// the cache into a permanent miss + break tag revalidation.
import { describe, expect, it } from "vitest";
import { cacheIdentity } from "@/lib/api";
import { DEMO_TENANT_ID } from "@/lib/demo";

function jwtWith(payload: object): string {
  const body = Buffer.from(JSON.stringify(payload)).toString("base64url");
  return `header.${body}.signature`;
}

describe("cacheIdentity", () => {
  it("passes the demo credential through unchanged", () => {
    expect(cacheIdentity(DEMO_TENANT_ID)).toBe(DEMO_TENANT_ID);
  });

  it("extracts the stable sub claim from an identity JWT", () => {
    expect(cacheIdentity(jwtWith({ sub: "idu-123", email: "a@b.c" }))).toBe("idu-123");
  });

  it("is stable across two different tokens for the same identity", () => {
    const a = jwtWith({ sub: "idu-123", exp: 1 });
    const b = jwtWith({ sub: "idu-123", exp: 2 });
    expect(cacheIdentity(a)).toBe(cacheIdentity(b));
  });

  it("throws on a credential that is neither the demo id nor a JWT", () => {
    expect(() => cacheIdentity("some-random-string")).toThrow();
  });

  it("throws on a JWT without a sub claim", () => {
    expect(() => cacheIdentity(jwtWith({ email: "a@b.c" }))).toThrow(/sub/);
  });
});
