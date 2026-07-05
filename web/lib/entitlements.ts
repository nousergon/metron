// Server-side entitlement resolution shared by every portfolio page.
//
// One source of truth for: (1) the nav lock state (PortfolioNav.featureStates) and
// (2) the full-page <Locked> gate when a gated route is hit directly. The owner-only
// tier-simulator preview (cookies) is forwarded to GET /meta/entitlements, which honors
// it ONLY when the simulator is enabled server-side — on the public product the cookies
// are ignored, so a normal user can't re-scope their own entitlements.

import { cookies } from "next/headers";
import { unstable_cache } from "next/cache";
import { getEntitlements, type Entitlement, type Entitlements } from "@/lib/api";
import type { NavFeatureState } from "@/components/portfolio-nav";

/** Short-TTL revalidation window for the entitlement flags (metron-ops#91 Part 2).
 *  These flags gate UI affordances only — the backend re-checks entitlements on every
 *  (no-store) data/compute call — so a flag that is up to this many seconds stale can
 *  never grant real access; the worst case is a feature toggle taking ≤TTL to reflect a
 *  tier change. Keeps `getEntitlements` off the every-nav round-trip without touching the
 *  live NAV/price paths (which stay `cache: "no-store"`). */
export const ENTITLEMENTS_REVALIDATE_SECONDS = 60;

/** Cache tag for one tenant's entitlements. A tier/billing change handler (e.g. a Stripe
 *  webhook) can call `revalidateTag(entitlementsTag(tenantId))` to drop the ≤TTL window to
 *  zero for that tenant; otherwise the TTL above bounds staleness. */
export const entitlementsTag = (tenantId: string) => `entitlements:${tenantId}`;

/** The owner-simulator preview selection (tier dropdown + feed toggle) from cookies.
 *  `cookies()` is async in Next 15, so this is now async and every caller must await it. */
export async function previewFromCookies(): Promise<{ tier?: string; feed?: boolean }> {
  const jar = await cookies();
  const tier = jar.get("metron_preview_tier")?.value;
  const feedRaw = jar.get("metron_preview_feed")?.value;
  return { tier, feed: feedRaw === undefined ? undefined : feedRaw === "true" };
}

/** Resolve entitlements for the current request (best-effort — null on backend error,
 *  so a transient failure degrades to "ungated" rather than blanking the page).
 *
 *  Read through a short-TTL `unstable_cache` so repeated navs reuse one fetch within the
 *  revalidate window. `cookies()` is read OUTSIDE the cached scope (it is request-dynamic
 *  and disallowed inside `unstable_cache`); the resolved tenant + preview are passed as
 *  EXPLICIT keyParts so cache isolation never depends on Next's header-based fetch keying
 *  (a documented cross-tenant footgun for header-authed multi-tenant apps). Distinct
 *  preview tiers/feeds key to distinct entries, so the owner tier-simulator stays exact. */
export async function loadEntitlements(tenantId: string): Promise<Entitlements | null> {
  const preview = await previewFromCookies();
  try {
    const read = unstable_cache(
      () => getEntitlements(tenantId, preview),
      [
        "entitlements",
        tenantId,
        preview.tier ?? "",
        preview.feed === undefined ? "" : String(preview.feed),
      ],
      { revalidate: ENTITLEMENTS_REVALIDATE_SECONDS, tags: [entitlementsTag(tenantId)] },
    );
    return await read();
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

/** Load + map in one call — the nav lock/hide state for PortfolioNav. Every portfolio
 *  page passes this so feed-dependent pages hide consistently in the beta (metron-ops#53). */
export async function navFeatureStates(tenantId: string): Promise<Record<string, NavFeatureState> | undefined> {
  return toFeatureStates(await loadEntitlements(tenantId));
}

/** One feature's full entitlement entry (for the full-page <Locked> gate). */
export function featureEntitlement(entitlements: Entitlements | null, key: string): Entitlement | undefined {
  return entitlements?.features.find((f) => f.key === key);
}
