// Server-side entitlement resolution shared by every portfolio page.
//
// One source of truth for: (1) the nav lock state (PortfolioNav.featureStates) and
// (2) the full-page <Locked> gate when a gated route is hit directly. The owner-only
// tier-simulator preview (cookies) is forwarded to GET /meta/entitlements, which honors
// it ONLY when the simulator is enabled server-side — on the public product the cookies
// are ignored, so a normal user can't re-scope their own entitlements.

import { cookies } from "next/headers";
import { getEntitlements, type Entitlement, type Entitlements } from "@/lib/api";
import type { NavFeatureState } from "@/components/portfolio-nav";

/** The owner-simulator preview selection (tier dropdown + feed toggle) from cookies. */
export function previewFromCookies(): { tier?: string; feed?: boolean } {
  const jar = cookies();
  const tier = jar.get("metron_preview_tier")?.value;
  const feedRaw = jar.get("metron_preview_feed")?.value;
  return { tier, feed: feedRaw === undefined ? undefined : feedRaw === "true" };
}

/** Resolve entitlements for the current request (best-effort — null on backend error,
 *  so a transient failure degrades to "ungated" rather than blanking the page). */
export async function loadEntitlements(tenantId: string): Promise<Entitlements | null> {
  try {
    return await getEntitlements(tenantId, previewFromCookies());
  } catch {
    return null;
  }
}

/** feature key → nav lock state for PortfolioNav. Undefined when entitlements are
 *  unavailable (the nav then shows every link — fail open, never hide on an error). */
export function toFeatureStates(
  entitlements: Entitlements | null,
): Record<string, NavFeatureState> | undefined {
  if (!entitlements) return undefined;
  return Object.fromEntries(
    entitlements.features.map((f) => [f.key, { available: f.available, required_tier: f.required_tier }]),
  );
}

/** One feature's full entitlement entry (for the full-page <Locked> gate). */
export function featureEntitlement(entitlements: Entitlements | null, key: string): Entitlement | undefined {
  return entitlements?.features.find((f) => f.key === key);
}
