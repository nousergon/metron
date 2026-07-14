import { cookies, headers } from "next/headers";
import { redirect } from "next/navigation";
import { cache } from "react";
import { DEMO_COOKIE, DEMO_TENANT_ID } from "@/lib/demo";

// Server-side session helpers over the SHARED nousergon-auth identity service
// (metron-ops#179). Metron no longer runs Better Auth in-process: the shared service
// at AUTH_URL owns sign-in and sessions (its cookie is set on `.nousergon.ai`, so it
// reaches this server-side tier on every request), and Metron's FastAPI backend
// verifies the short-lived JWTs the service mints. Pages/actions call
// `requireApiAuth()` to get the credential `lib/api.ts` sends — or get redirected to
// /login.

export const AUTH_URL = process.env.NEXT_PUBLIC_AUTH_URL ?? "https://auth.nousergon.ai";

export type SharedSession = {
  user: { id: string; email: string; name?: string | null };
} | null;

/** The shared-service session for the incoming request (JSON `null` when signed out).
 * Forwards the request's own cookie header — the better-auth session cookie lives on
 * the parent domain. Cached per request render. */
export const getSession = cache(async (): Promise<SharedSession> => {
  const cookie = headers().get("cookie");
  if (!cookie) return null;
  const res = await fetch(`${AUTH_URL}/api/auth/get-session`, {
    headers: { cookie },
    cache: "no-store",
  });
  if (!res.ok) {
    // The auth service being unreachable/broken is an operational failure — surface
    // it, never silently treat the user as signed out.
    throw new Error(`auth get-session → ${res.status}`);
  }
  return (await res.json()) as SharedSession;
});

/** The shared service's session-cookie name — presence of this cookie means the
 * browser BELIEVES it has a session, which changes what a failed mint means (see
 * requireApiAuth). `__Secure-` prefix matches the service's https baseURL. */
const SESSION_COOKIE = "__Secure-better-auth.session_token";

/** The signed-in user's API credential: a short-lived identity JWT minted by the
 * shared service (GET /api/auth/token, authenticated by its session cookie), which
 * the FastAPI backend verifies against the service's JWKS and resolves to the user's
 * workspace (tenant). Falls back to the READ-ONLY demo credential — the fixed demo
 * tenant id, the one X-Tenant-Id value the backend still accepts — ONLY for visitors
 * with no session cookie at all (a prospect exploring via the `/demo` cookie). A
 * request that CARRIES a session cookie but fails the mint gets /login, never demo:
 * during the 2026-07-11 cutover verification a signed-in user was silently shown the
 * demo tenant's data because a deploy-window mint failure fell through to a stale
 * `metron_demo` cookie (metron-ops#183) — wrong-tenant data is the worst rendering
 * of an auth hiccup. Cached per request render so one page doesn't mint per fetch. */
export const requireApiAuth = cache(async (): Promise<string> => {
  const hasSessionCookie = cookies().get(SESSION_COOKIE) !== undefined;
  const cookie = headers().get("cookie");
  if (cookie && hasSessionCookie) {
    const res = await fetch(`${AUTH_URL}/api/auth/token`, {
      headers: { cookie },
      cache: "no-store",
    });
    if (res.ok) {
      const { token } = (await res.json()) as { token: string };
      return token;
    }
    if (res.status !== 401 && res.status !== 403) {
      // Anything but 401/403 means the auth service itself is failing: fail loud.
      throw new Error(`auth token mint → ${res.status}`);
    }
    // 401/403 with a session cookie present = the session is stale/invalid (or the
    // service is mid-restart). The honest state is "please sign in again" — never
    // the demo fallback, which would silently show another tenant's data.
    redirect("/login");
  }
  if (cookies().get(DEMO_COOKIE)?.value === "1") return DEMO_TENANT_ID;
  redirect("/login");
});
