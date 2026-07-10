// Demo / sample portfolio (metron-ops#42). A prospect can open `/demo` to explore the
// product read-only with no signup/connection: the route sets a cookie that
// `requireTenantId` resolves to the fixed demo tenant (seeded server-side, read-only).
// These ids mirror api/services/demo.py — keep them in lockstep.

export const DEMO_TENANT_ID = "00000000-0000-0000-0000-00000000de60";
// The single read-only showcase under the demo tenant, also visible on every real
// tenant's own dashboard: the "Showcase Portfolio" — a live sleeve synced daily from
// the engine's published artifact, plus a permanently-frozen sample sleeve for
// asset-class/tax breadth (formerly a separate "Demo portfolio"; merged in to cut
// showcase-portfolio clutter). Illustrative only, no performance claims, no stated
// objective. Renamed from "Reference Rate" (collided with the established, unrelated
// finance term for a benchmark interest rate) — the id/const name below is unchanged.
// Mirrors api/services/demo.py::REFERENCE_PORTFOLIO_ID — keep in lockstep.
export const REFERENCE_PORTFOLIO_ID = "00000000-0000-0000-0000-00000000de62";
export const DEMO_COOKIE = "metron-demo";

// The illustrative-only notice shown on the Showcase Portfolio. No performance claim
// and no stated objective — it is a demo/illustrative-only showcase.
export const REFERENCE_DISCLAIMER =
  "Showcase portfolio — illustrative only. Not investment advice; no representation is made as to performance.";

export function isDemoTenant(tenantId: string | null | undefined): boolean {
  return tenantId === DEMO_TENANT_ID;
}

export function isReferencePortfolio(portfolioId: string | null | undefined): boolean {
  return portfolioId === REFERENCE_PORTFOLIO_ID;
}
