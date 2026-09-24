// External user demo (metron-ops-I310). An invited viewer redeems a single-use code at
// `/invite?code=…`; the backend returns an opaque session token, kept here in an httpOnly
// cookie and sent to the API as `X-Demo-Session`. The API pins that session server-side
// to the no-advice feature set over the demo household (api/services/external_demo.py) —
// everything in this file is navigation convenience; the backend is the gate.

export const EXTERNAL_DEMO_COOKIE = "metron-xdemo";
/** Credential prefix `requireApiAuth` returns for an external-demo session. */
export const EXTERNAL_DEMO_CREDENTIAL_PREFIX = "xdemo:";
/** Mirrors api/services/demo_household.py::DEMO_HOUSEHOLD_PORTFOLIO_ID — keep in lockstep. */
export const DEMO_HOUSEHOLD_PORTFOLIO_ID = "00000000-0000-0000-0000-00000000de63";
export const EXTERNAL_DEMO_LANDING = `/portfolios/${DEMO_HOUSEHOLD_PORTFOLIO_ID}/glance`;

export function isExternalDemoCredential(apiAuth: string): boolean {
  return apiAuth.startsWith(EXTERNAL_DEMO_CREDENTIAL_PREFIX);
}

export function externalDemoToken(apiAuth: string): string {
  return apiAuth.slice(EXTERNAL_DEMO_CREDENTIAL_PREFIX.length);
}

// Household pages an external viewer may open. Everything else redirects to the landing
// page (default deny): advice pages, settings, crypto, other portfolios.
const ALLOWED_SEGMENTS = new Set([
  "glance",
  "overview",
  "diagnostics",
  "performance",
  "risk",
  "attribution",
  "tax",
  "macro",
  "calendar",
  "plan",
  "watchlist",
  "tearsheet",
  "in-development",
]);

/** Whether an external-demo viewer may open `pathname` (basePath already stripped). */
export function isExternalDemoPageAllowed(pathname: string): boolean {
  if (pathname === "/invite") return true;
  // Static measurement documentation (metron-ops-I205) — no data, no advice; the analytics
  // pages' "How this is measured" links land here.
  if (pathname === "/methodology") return true;
  const base = `/portfolios/${DEMO_HOUSEHOLD_PORTFOLIO_ID}`;
  if (pathname === base) return true;
  if (!pathname.startsWith(`${base}/`)) return false;
  const segment = pathname.slice(base.length + 1).split("/")[0];
  return ALLOWED_SEGMENTS.has(segment);
}
