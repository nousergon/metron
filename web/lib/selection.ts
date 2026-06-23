// Server-side resolution of the accounts-panel selection for pages that scope by
// the repeatable `?account_id=` query.

import { redirect } from "next/navigation";
import { acctParams, getAccountSelection } from "@/lib/api";

/** Resolve a page's account selection: an explicit `?account_id=` in the URL always
 * wins; with none, the saved panel selection (InvestorPreferences) is applied by
 * redirecting into the equivalent URL — so the panel checkboxes, cross-page nav
 * links, and sub-page scoping all keep running off the single URL mechanism.
 *
 * `applySaved: false` skips the saved-selection restore: with no `?account_id=` the
 * page defaults to the WHOLE portfolio (every account on). The Overview uses this —
 * it's the portfolio summary, so it should anchor on the full total rather than land
 * scoped into a stale saved filter (which makes the headline value look off).
 *
 * Best-effort on the saved-selection fetch (a prefs failure must never break the
 * page). NOTE: calls `redirect()` — invoke OUTSIDE any try/catch in the page. */
export async function resolveAccountIds(
  tenantId: string,
  portfolioId: string,
  basePath: string,
  raw: string | string[] | undefined,
  { applySaved = true }: { applySaved?: boolean } = {},
): Promise<string[]> {
  if (raw != null) return Array.isArray(raw) ? raw : [raw];
  if (!applySaved) return [];
  let saved: string[] = [];
  try {
    saved = await getAccountSelection(tenantId, portfolioId);
  } catch {
    saved = [];
  }
  if (saved.length > 0) redirect(`${basePath}${acctParams(saved)}`);
  return [];
}
