"use client";

// Global SWR cache config (metron-ops#232) — one client-side cache shared by every hook
// under lib/use-*.ts, so navigating between pages that read the same key serves instantly
// from cache instead of re-fetching. Defaults kept conservative (revalidate on focus/
// reconnect, no automatic polling) — components opt into a poll interval per key.

import type { ReactNode } from "react";
import { SWRConfig } from "swr";

export function SwrProvider({ children }: { children: ReactNode }) {
  return (
    <SWRConfig
      value={{
        revalidateOnFocus: true,
        revalidateOnReconnect: true,
        dedupingInterval: 5000,
      }}
    >
      {children}
    </SWRConfig>
  );
}
