"use client";

// SWR-backed crypto addresses cache (metron-ops#232) — the page's SSR fetch seeds
// `fallbackData` so first paint is instant; a mutation calls `mutate()` to revalidate
// just this key instead of `router.refresh()` re-rendering the whole Server Component page.

import useSWR from "swr";
import type { CryptoSummary } from "@/lib/api";
import { fetchCryptoAction } from "@/app/portfolios/[id]/actions";

export function cryptoKey(portfolioId: string): string {
  return `crypto:${portfolioId}`;
}

export function useCrypto(portfolioId: string, fallbackData: CryptoSummary) {
  return useSWR<CryptoSummary>(
    cryptoKey(portfolioId),
    () => fetchCryptoAction(portfolioId),
    { fallbackData, revalidateOnFocus: true },
  );
}
