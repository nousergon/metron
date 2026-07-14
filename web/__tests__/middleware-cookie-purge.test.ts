// Legacy host-only session-cookie purge (metron-ops#179 diagnosis): a request
// carrying TWO cookies with the shared session-cookie name is the signature of the
// embedded-era leftover shadowing the shared service's cookie.
import { describe, it, expect } from "vitest";
import { hasShadowedSessionCookie } from "../middleware";

const NAME = "__Secure-better-auth.session_token";

describe("hasShadowedSessionCookie", () => {
  it("detects the legacy+shared duplicate pair", () => {
    expect(hasShadowedSessionCookie(`${NAME}=legacy.sig; metron-demo=1; ${NAME}=shared.sig`)).toBe(
      true,
    );
  });

  it("passes a single (healthy) session cookie through", () => {
    expect(hasShadowedSessionCookie(`${NAME}=shared.sig; CF_Authorization=abc`)).toBe(false);
  });

  it("ignores cookie-less requests and unrelated cookies", () => {
    expect(hasShadowedSessionCookie(null)).toBe(false);
    expect(hasShadowedSessionCookie("metron-demo=1")).toBe(false);
  });

  it("does not false-positive on a name that merely shares the prefix", () => {
    expect(hasShadowedSessionCookie(`${NAME}=a.sig; ${NAME}-something=b`)).toBe(false);
  });
});
