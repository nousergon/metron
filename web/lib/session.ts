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

/** The signed-in user's API credential: a short-lived identity JWT minted by the
 * shared service (GET /api/auth/token, authenticated by its session cookie), which
 * the FastAPI backend verifies against the service's JWKS and resolves to the user's
 * workspace (tenant). Falls back to the READ-ONLY demo credential — the fixed demo
 * tenant id, the one X-Tenant-Id value the backend still accepts — when the `/demo`
 * cookie is set with no auth session, so a prospect can explore without signing up; a
 * real session always wins. Otherwise redirects to /login. Cached per request render
 * so one page doesn't mint a token per fetch. */
export const requireApiAuth = cache(async (): Promise<string> => {
  const cookie = headers().get("cookie");
  if (cookie) {
    const res = await fetch(`${AUTH_URL}/api/auth/token`, {
      headers: { cookie },
      cache: "no-store",
    });
    if (res.ok) {
      const { token } = (await res.json()) as { token: string };
      return token;
    }
    if (res.status !== 401 && res.status !== 403) {
      // 401/403 = "no shared session" (expected — fall through to demo/login).
      // Anything else means the auth service itself is failing: fail loud.
      throw new Error(`auth token mint → ${res.status}`);
    }
  }
  if (cookies().get(DEMO_COOKIE)?.value === "1") return DEMO_TENANT_ID;
  redirect("/login");
});
