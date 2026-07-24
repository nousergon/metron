"use client";

// SWR-backed holdings poller (metron-ops#232) — rendered as an invisible sibling in the
// portfolio page, keeps the SWR cache warm at the right cadence for the current valuation
// mode. Replaces the blanket `router.refresh()` that SettledRefresher fired on a timer:
// the holdings data now re-fetches through SWR's `refreshInterval` directly, leaving other
// page state untouched.
//
// Renders nothing — one hook call, no DOM.

import { useHoldings } from "@/lib/use-holdings";

const LIVE_MS = 5 * 60 * 1000;  // revalue holdings from delayed intraday quotes
const SETTLED_MS = 30 * 60 * 1000; // catch the once-daily EOD snapshot advance

export function HoldingsPoller({
  portfolioId,
  accountIds,
  byAccount,
  valuation,
}: {
  portfolioId: string;
  accountIds: string[];
  byAccount: boolean;
  valuation: "live" | "settled";
}) {
  // This hook:
  //   1. Seeds the SWR cache with the SSR data (first paint is instant via the
  //      HoldingsSection's fallback prop — see page.tsx).
  //   2. Re-fetches on the chosen interval — no blanket `router.refresh()` needed.
  //   3. Exposes `mutate()` for targeted cache revalidation after a user mutation.
  useHoldings(portfolioId, accountIds, byAccount, valuation, [], valuation === "live" ? LIVE_MS : SETTLED_MS);

  return null;
}
