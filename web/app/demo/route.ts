import { NextResponse } from "next/server";
import { DEMO_COOKIE, REFERENCE_PORTFOLIO_ID } from "@/lib/demo";
import { track } from "@/lib/track";

// `/demo` — open the read-only Showcase Portfolio with no signup. Sets the demo
// cookie (so requireApiAuth resolves the demo tenant) and redirects into it. The data
// is seeded server-side and the API refuses writes to the demo tenant (metron-ops#42).
// Was its own separate frozen "Demo portfolio" fixture; merged into the Showcase
// Portfolio to cut showcase-portfolio clutter (see api/services/demo.py docstring).
export async function GET(req: Request) {
  // Funnel entry: a prospect opening the live demo (metron-ops#34). Best-effort — the
  // await never blocks the redirect on a slow/unreachable sink.
  await track("demo_viewed");
  const res = NextResponse.redirect(new URL(`/portfolios/${REFERENCE_PORTFOLIO_ID}`, req.url));
  res.cookies.set(DEMO_COOKIE, "1", { httpOnly: true, sameSite: "lax", path: "/" });
  return res;
}

// `/demo` POST clears the demo cookie (exit demo → back to sign-in).
export function POST(req: Request) {
  const res = NextResponse.redirect(new URL("/login", req.url));
  res.cookies.set(DEMO_COOKIE, "", { httpOnly: true, sameSite: "lax", path: "/", maxAge: 0 });
  return res;
}
