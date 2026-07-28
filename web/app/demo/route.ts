import { NextResponse } from "next/server";
import { DEMO_COOKIE, REFERENCE_PORTFOLIO_ID } from "@/lib/demo";
import { withBasePath } from "@/lib/base-path";
import { track } from "@/lib/track";

// A RELATIVE Location header, deliberately — not NextResponse.redirect().
//
// NextResponse.redirect() requires an ABSOLUTE URL, and the only origin available to
// a route handler is `req.url`, which behind the Cloudflare Worker → nginx → :3003
// chain is the INTERNAL address. That is what shipped, and `/dash/demo` publicly
// answered `307 Location: https://localhost:3003/portfolios/...` — a dead link that
// also leaked the internal origin.
//
// RFC 7231 §7.1.2 allows a relative Location; the browser resolves it against the
// request URL, so the public origin is preserved without this handler having to
// reconstruct it from (spoofable, and here not-even-forwarded) Host headers. The
// basePath still has to be added by hand — route handlers get no auto-prefixing.
function redirectTo(path: string, status: 303 | 307): NextResponse {
  return new NextResponse(null, { status, headers: { Location: withBasePath(path) } });
}

// `/demo` — open the read-only Showcase Portfolio with no signup. Sets the demo
// cookie (so requireApiAuth resolves the demo tenant) and redirects into it. The data
// is seeded server-side and the API refuses writes to the demo tenant (metron-ops#42).
// Was its own separate frozen "Demo portfolio" fixture; merged into the Showcase
// Portfolio to cut showcase-portfolio clutter (see api/services/demo.py docstring).
export async function GET() {
  // Funnel entry: a prospect opening the live demo (metron-ops#34). Best-effort — the
  // await never blocks the redirect on a slow/unreachable sink.
  await track("demo_viewed");
  const res = redirectTo(`/portfolios/${REFERENCE_PORTFOLIO_ID}`, 307);
  res.cookies.set(DEMO_COOKIE, "1", { httpOnly: true, sameSite: "lax", path: "/" });
  return res;
}

// `/demo` POST clears the demo cookie (exit demo → back to sign-in).
//
// 303, not 307: 307 PRESERVES the method, so the "Exit demo" button re-POSTed to
// /login — a page route, which answers POST with 405. 303 is the status defined for
// exactly this POST→GET hand-off.
export function POST() {
  const res = redirectTo("/login", 303);
  res.cookies.set(DEMO_COOKIE, "", { httpOnly: true, sameSite: "lax", path: "/", maxAge: 0 });
  return res;
}
