// Demo / sample portfolio (metron-ops#42). A prospect can open `/demo` to explore the
// product read-only with no signup/connection: the route sets a cookie that
// `requireTenantId` resolves to the fixed demo tenant (seeded server-side, read-only).
// These ids mirror api/services/demo.py — keep them in lockstep.

export const DEMO_TENANT_ID = "00000000-0000-0000-0000-00000000de60";
export const DEMO_PORTFOLIO_ID = "00000000-0000-0000-0000-00000000de61";
// A second, LIVE read-only showcase under the demo tenant: the "Reference Rate" —
// an illustrative reference portfolio synced daily from the engine's published artifact.
// Illustrative only, no performance claims, no stated objective. Mirrors
// api/services/demo.py::REFERENCE_PORTFOLIO_ID — keep in lockstep.
export const REFERENCE_PORTFOLIO_ID = "00000000-0000-0000-0000-00000000de62";
export const DEMO_COOKIE = "metron-demo";

// The illustrative-only notice shown on the Reference Rate portfolio. No performance
// claim and no stated objective — it is a "reference rate", demo/illustrative only.
export const REFERENCE_DISCLAIMER =
  "Reference rate — illustrative only. Not investment advice; no representation is made as to performance.";

export function isDemoTenant(tenantId: string | null | undefined): boolean {
  return tenantId === DEMO_TENANT_ID;
}

export function isReferencePortfolio(portfolioId: string | null | undefined): boolean {
  return portfolioId === REFERENCE_PORTFOLIO_ID;
}
