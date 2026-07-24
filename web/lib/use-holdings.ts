"use client";

// SWR-backed holdings cache (metron-ops#232) — the page's SSR fetch seeds `fallbackData`
// so first paint is instant; a mutation calls `mutate()` to revalidate just this key
// instead of `router.refresh()` re-rendering the whole Server Component page. Polling
// via `refreshInterval` replaces the blanket timer in IntradayRefresher/SettledRefresher.

import useSWR from "swr";
import type { Holding } from "@/lib/api";
import { fetchHoldingsAction } from "@/app/portfolios/[id]/actions";

export function holdingsKey(portfolioId: string): string {
  return `holdings:${portfolioId}`;
}

export function useHoldings(
  portfolioId: string,
  accountIds: string[],
  byAccount: boolean,
  valuation: "live" | "settled",
  fallbackData: Holding[],
  refreshIntervalMs: number = 0,
) {
  return useSWR<Holding[]>(
    holdingsKey(portfolioId),
    () => fetchHoldingsAction(portfolioId, accountIds, byAccount, valuation),
    { fallbackData, refreshInterval: refreshIntervalMs, revalidateOnFocus: true },
  );
}
