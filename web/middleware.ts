import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

// Purge the LEGACY host-only session cookie left over from Metron's embedded
// better-auth era (pre metron-ops#179 cutover).
//
// The embedded instance set `__Secure-better-auth.session_token` as a HOST-ONLY
// cookie on portfolio.nousergon.ai, signed with Metron's own (now retired) secret.
// The shared nousergon-auth service sets a cookie with the SAME NAME but scoped to
// `.nousergon.ai` and signed with ITS secret. A browser that carries both sends the
// host-only one first, so the auth service verifies the stale signature, rejects it,
// and a genuinely signed-in user gets bounced to /login on every request — which is
// exactly what happened during the 2026-07-11 cutover verification (diagnosis on
// metron-ops#179).
//
// A duplicate cookie name in one request is the unambiguous signature of that state:
// expire the host-only copy (a Set-Cookie WITHOUT a Domain attribute only ever
// matches the host-only cookie, so the shared `.nousergon.ai` session survives).
// Steady-state (single cookie) requests pass through untouched.

const SESSION_COOKIE = "__Secure-better-auth.session_token";

export function hasShadowedSessionCookie(cookieHeader: string | null): boolean {
  if (!cookieHeader) return false;
  return cookieHeader.split(/;\s*/).filter((c) => c.startsWith(`${SESSION_COOKIE}=`)).length >= 2;
}

export function middleware(request: NextRequest) {
  const response = NextResponse.next();
  if (hasShadowedSessionCookie(request.headers.get("cookie"))) {
    // Name + Path only, deliberately NO Domain: deletes the host-only legacy
    // cookie and cannot touch the shared service's `.nousergon.ai` cookie.
    response.headers.append(
      "set-cookie",
      `${SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax`,
    );
  }
  return response;
}

export const config = {
  // Static assets never need cookie surgery; everything else (pages, server
  // actions, route handlers) gets the purge check.
  matcher: ["/((?!_next/static|_next/image|favicon).*)"],
};
