// Demo / sample portfolio (metron-ops#42). A prospect can open `/demo` to explore the
// product read-only with no signup/connection: the route sets a cookie that
// `requireTenantId` resolves to the fixed demo tenant (seeded server-side, read-only).
// These ids mirror api/services/demo.py — keep them in lockstep.

export const DEMO_TENANT_ID = "00000000-0000-0000-0000-00000000de60";
export const DEMO_PORTFOLIO_ID = "00000000-0000-0000-0000-00000000de61";
export const DEMO_COOKIE = "metron-demo";

export function isDemoTenant(tenantId: string | null | undefined): boolean {
  return tenantId === DEMO_TENANT_ID;
}
