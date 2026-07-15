import "server-only";
import { cookies, type UnsafeUnwrappedCookies } from "next/headers";

// Self-hosted analytics — the web tier's thin wrapper over the FastAPI `/track` sink
// (metron-ops#34). No third-party tracker: Cloudflare Web Analytics already covers
// page-views; this is the EVENT tier for the beta funnel (waitlist → signup → activation).
//
// Server-only by construction: `track()` runs in Server Actions / route handlers, so the
// event is posted server-to-server and the anonymous session id lives in an httpOnly
// cookie the browser can't read or forge. The later product surfaces (#30 core read pages,
// #32 onboarding) call `track("<new_event>")` and the sink stores + funnels it with no
// schema or endpoint change.

const API_URL = process.env.METRON_API_URL ?? "http://localhost:8000";

// The anonymous funnel key. httpOnly (not readable by client JS — it's a funnel id, not a
// tracker fingerprint), lax same-site, 1-year TTL so a returning visitor's pre-auth events
// stitch to the same session. Named distinctly from the auth/demo cookies.
export const SESSION_COOKIE = "metron-sid";
const SESSION_TTL_SECONDS = 60 * 60 * 24 * 365;

function newSessionId(): string {
  // Opaque, non-PII. `crypto.randomUUID` is available in the Node/Edge runtimes Next runs.
  return crypto.randomUUID();
}

/** Read the caller's anonymous session id, minting + persisting one on first sight. Safe to
 *  call from any Server Action / route handler; the cookie is httpOnly so it never leaks to
 *  client script. */
export function sessionId(): string {
  const jar = (cookies() as unknown as UnsafeUnwrappedCookies);
  const existing = jar.get(SESSION_COOKIE)?.value;
  if (existing) return existing;
  const sid = newSessionId();
  jar.set(SESSION_COOKIE, sid, {
    httpOnly: true,
    sameSite: "lax",
    path: "/",
    maxAge: SESSION_TTL_SECONDS,
  });
  return sid;
}

/** Fire one analytics event into the self-hosted sink. Best-effort by design: analytics
 *  must NEVER break a funnel step, so a failed/unreachable backend is swallowed (the funnel
 *  read simply won't count the miss) rather than surfaced to the user. `userId` is attached
 *  only once the visitor is authenticated; `props` is arbitrary JSON context. */
export async function track(
  eventName: string,
  props: Record<string, unknown> = {},
  userId?: string,
): Promise<void> {
  try {
    await fetch(`${API_URL}/track`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        event_name: eventName,
        session_id: sessionId(),
        user_id: userId ?? null,
        props,
      }),
      cache: "no-store",
    });
  } catch {
    // Best-effort: never let instrumentation break the user's flow.
  }
}
