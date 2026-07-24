"use client";

// SWR-backed intraday status (metron-ops#232) — polled on a timer so the "as of" label
// stays fresh. This replaces the ad-hoc useEffect poll inside IntradayRefresher, keeping
// the SWR cache consistent so other components can also read the status without re-fetching.

import useSWR from "swr";
import type { IntradayStatus } from "@/lib/api";
import { fetchIntradayStatusAction } from "@/app/portfolios/[id]/intraday-action";

export function intradayKey(portfolioId: string): string {
  return `intraday:${portfolioId}`;
}

export function useIntradayStatus(portfolioId: string, refreshIntervalMs: number = 0) {
  return useSWR<IntradayStatus | null>(
    intradayKey(portfolioId),
    () => fetchIntradayStatusAction(portfolioId),
    { refreshInterval: refreshIntervalMs, revalidateOnFocus: false },
  );
}
