"use client";

// SWR-backed watchlist cache (metron-ops#232) — the page's SSR fetch seeds `fallbackData`
// so first paint is instant; a mutation calls `mutate()` to revalidate just this key
// instead of `router.refresh()` re-rendering the whole Server Component page.

import useSWR from "swr";
import type { WatchlistEntry } from "@/lib/api";
import { fetchWatchlistAction } from "@/app/portfolios/[id]/actions";

export function watchlistKey(portfolioId: string): string {
  return `watchlist:${portfolioId}`;
}

export function useWatchlist(portfolioId: string, fallbackData: WatchlistEntry[]) {
  return useSWR<WatchlistEntry[]>(watchlistKey(portfolioId), () => fetchWatchlistAction(portfolioId), {
    fallbackData,
    revalidateOnFocus: true,
  });
}
