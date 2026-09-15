import { NextResponse, type NextRequest } from "next/server";
import { apiFetch } from "@/lib/api";
import { withBasePath } from "@/lib/base-path";
import { EXTERNAL_DEMO_COOKIE, EXTERNAL_DEMO_LANDING } from "@/lib/external-demo";

// `/invite?code=…` — redeem a single-use external-demo invite (metron-ops-I310). A route
// handler, not a page: it renders no content, so no unauthenticated route gains any. The
// backend refuses redemption (403) until the demo is released, and on any refusal the
// visitor lands on /login exactly as if the link did not exist.
//
// Relative Location + manual basePath, for the reason documented in app/demo/route.ts.
function redirectTo(path: string, status: 303 | 307): NextResponse {
  return new NextResponse(null, {
    status,
    headers: {
      Location: withBasePath(path),
      // The code is single-use, but never hand it to a third party via Referer, and never cache.
      "Referrer-Policy": "no-referrer",
      "Cache-Control": "no-store",
    },
  });
}

export async function GET(req: NextRequest) {
  const code = req.nextUrl.searchParams.get("code")?.trim() ?? "";
  if (!code || code.length > 128) return redirectTo("/login", 307);
  let res: Response;
  try {
    res = await apiFetch("/external-demo/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code }),
      cache: "no-store",
    });
  } catch {
    return redirectTo("/login", 307);
  }
  if (!res.ok) return redirectTo("/login", 307);
  const { token, expires_at } = (await res.json()) as { token: string; expires_at: string };
  const out = redirectTo(EXTERNAL_DEMO_LANDING, 307);
  out.cookies.set(EXTERNAL_DEMO_COOKIE, token, {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "lax",
    path: "/",
    // The API serialises naive UTC timestamps; mark them as UTC explicitly.
    expires: new Date(expires_at.endsWith("Z") ? expires_at : `${expires_at}Z`),
  });
  return out;
}

// POST clears the external-demo session cookie (exit the demo). 303 for the POST→GET hand-off.
export function POST() {
  const out = redirectTo("/login", 303);
  out.cookies.set(EXTERNAL_DEMO_COOKIE, "", { httpOnly: true, sameSite: "lax", path: "/", maxAge: 0 });
  return out;
}
