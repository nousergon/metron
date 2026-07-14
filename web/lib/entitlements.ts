// Server-side entitlement resolution shared by every portfolio page.
//
// One source of truth for: (1) the nav lock state (PortfolioNav.featureStates) and
// (2) the full-page <Locked> gate when a gated route is hit directly. The owner-only
// tier-simulator preview (cookies) is forwarded to GET /meta/entitlements, which honors
// it ONLY when the simulator is enabled server-side — on the public product the cookies
// are ignored, so a normal user can't re-scope their own entitlements.

import { cookies, type UnsafeUnwrappedCookies } from "next/headers";
import { unstable_cache } from "next/cache";
import { cacheIdentity, getEntitlements, type Entitlement, type Entitlements } from "@/lib/api";
import type { NavFeatureState } from "@/components/portfolio-nav";

/** Short-TTL revalidation window for the entitlement flags (metron-ops#91 Part 2).
 *  These flags gate UI affordances only — the backend re-checks entitlements on every
 *  (no-store) data/compute call — so a flag that is up to this many seconds stale can
 *  never grant real access; the worst case is a feature toggle taking ≤TTL to reflect a
 *  tier change. Keeps `getEntitlements` off the every-nav round-trip without touching the
 *  live NAV/price paths (which stay `cache: "no-store"`). */
export const ENTITLEMENTS_REVALIDATE_SECONDS = 60;

/** Cache tag for one tenant's entitlements. A tier/billing change handler (e.g. a Stripe
 *  webhook) can call `revalidateTag(entitlementsTag(apiAuth))` to drop the ≤TTL window to
 *  zero for that tenant; otherwise the TTL above bounds staleness. Keyed on the STABLE
 *  identity behind the credential (see `cacheIdentity`), never the short-lived JWT. */
export const entitlementsTag = (apiAuth: string) => `entitlements:${cacheIdentity(apiAuth)}`;

/** The owner-simulator preview selection (tier dropdown + feed toggle) from cookies. */
export function previewFromCookies(): { tier?: string; feed?: boolean } {
  const jar = (cookies() as unknown as UnsafeUnwrappedCookies);
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
 *  (a documented cross-tenant footgun for header-authed multi-tenant apps). The identity
 *  keyPart is the stable `cacheIdentity(apiAuth)` — NOT the raw credential, which is a
 *  short-lived JWT re-minted per render (metron-ops#179) and would never hit. Distinct
 *  preview tiers/feeds key to distinct entries, so the owner tier-simulator stays exact. */
export async function loadEntitlements(apiAuth: string): Promise<Entitlements | null> {
  const preview = previewFromCookies();
  try {
    const read = unstable_cache(
      () => getEntitlements(apiAuth, preview),
      [
        "entitlements",
        cacheIdentity(apiAuth),
        preview.tier ?? "",
        preview.feed === undefined ? "" : String(preview.feed),
      ],
      { revalidate: ENTITLEMENTS_REVALIDATE_SECONDS, tags: [entitlementsTag(apiAuth)] },
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
export async function navFeatureStates(apiAuth: string): Promise<Record<string, NavFeatureState> | undefined> {
  return toFeatureStates(await loadEntitlements(apiAuth));
}

/** One feature's full entitlement entry (for the full-page <Locked> gate). */
export function featureEntitlement(entitlements: Entitlements | null, key: string): Entitlement | undefined {
  return entitlements?.features.find((f) => f.key === key);
}
