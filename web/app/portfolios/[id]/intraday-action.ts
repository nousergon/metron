"use server";

// Server Action behind the live-NAV refresher (metron-ops#79): re-fetch the portfolio's
// intraday-valuation status. The client polls this every ~5 min while Metron is open; the
// request flows through the API's portfolio-ownership dependency, which touches the
// data-spine UI heartbeat — so an open app keeps the intraday quote producer publishing.

import { getIntradayStatus, type IntradayStatus } from "@/lib/api";
import { requireApiAuth } from "@/lib/session";

export async function fetchIntradayStatusAction(id: string): Promise<IntradayStatus | null> {
  try {
    const apiAuth = await requireApiAuth();
    return await getIntradayStatus(apiAuth, id);
  } catch {
    return null; // transient — keep the last label, don't break the page
  }
}
