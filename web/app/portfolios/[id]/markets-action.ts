"use server";

// Server Action for the Overview "markets" strip: re-fetch the latest intraday index
// levels (SPY/QQQ/IWM proxies). The client component polls this every ~5 min. The owner
// tier-simulator preview (cookies) is forwarded so a feed-off preview locks consistently
// with the page-level gate; honored server-side only when the simulator is on.

import { getIndices, type Indices } from "@/lib/api";
import { previewFromCookies } from "@/lib/entitlements";
import { requireApiAuth } from "@/lib/session";

export async function fetchIndicesAction(): Promise<Indices | null> {
  try {
    const apiAuth = await requireApiAuth();
    return await getIndices(apiAuth, previewFromCookies());
  } catch {
    return null; // transient — the client keeps showing the last good snapshot
  }
}
