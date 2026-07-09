// Short-TTL cache for account SELECTOR metadata (metron-ops#91 Part 2, operator
// decision 2026-07-08). `getAccounts` stays `cache: "no-store"` because it carries live
// per-account market_value/day_pct; the slim `GET /accounts/meta` endpoint carries only
// tag fields (name/nickname/institution/tax_treatment/taxable), so it can be cached
// without any stale-NAV risk. Consumers that only need those fields (e.g. the Settings
// account-tag table) should read through `loadAccountsMeta` instead of `getAccounts`.

import { unstable_cache } from "next/cache";
import { getAccountsMeta, type AccountMeta } from "@/lib/api";

/** Mirrors ENTITLEMENTS_REVALIDATE_SECONDS — a tag edit can take up to this long to
 *  show elsewhere unless the mutating action calls `revalidateTag(accountsMetaTag(...))`
 *  (the account-tag/delete/restore actions in app/portfolios/[id]/actions.ts all do). */
export const ACCOUNTS_META_REVALIDATE_SECONDS = 60;

/** Cache tag for one portfolio's account metadata, scoped by tenant too (tenant_id is
 *  not derivable from portfolio_id alone, and cache isolation must not rely on it being
 *  so). Mutating actions call `revalidateTag(accountsMetaTag(tenantId, portfolioId))`. */
export const accountsMetaTag = (tenantId: string, portfolioId: string) =>
  `accounts-meta:${tenantId}:${portfolioId}`;

/** Resolve account selector metadata for the current request (best-effort — null on
 *  backend error, so a transient failure degrades to "no accounts" rather than a hard
 *  page error). Read through a short-TTL `unstable_cache` keyed on EXPLICIT
 *  [tenantId, portfolioId] keyParts (not Next's header-based fetch keying — the same
 *  cross-tenant-footgun avoidance as loadEntitlements). */
export async function loadAccountsMeta(tenantId: string, portfolioId: string): Promise<AccountMeta[] | null> {
  try {
    const read = unstable_cache(
      () => getAccountsMeta(tenantId, portfolioId),
      ["accounts-meta", tenantId, portfolioId],
      { revalidate: ACCOUNTS_META_REVALIDATE_SECONDS, tags: [accountsMetaTag(tenantId, portfolioId)] },
    );
    return await read();
  } catch {
    return null;
  }
}
