"use client";

// Live-NAV refresher (metron-ops#79). While Metron is open, every ~5 min it:
//   1. re-fetches the intraday-valuation status (also pinging the data-spine UI heartbeat
//      via the API, so the producer keeps publishing while someone is looking), and
//   2. calls router.refresh() so the server components re-render — the headline NAV and
//      every position value recompute from the fresh intraday balances.
// It renders a small honest label ("intraday · ~15-min delayed · as of HH:MM") only while
// the overlay is actually applied (feed-entitled + a fresh snapshot). On the no-feed beta
// or after the close (stale) it renders nothing and the page shows EOD-close values.

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import type { IntradayStatus } from "@/lib/api";
import { fetchIntradayStatusAction } from "@/app/portfolios/[id]/intraday-action";

const REFRESH_MS = 5 * 60 * 1000;

/** "as of 11:03 AM" in the viewer's local time, from the artifact's UTC write time. */
function asOf(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
}

export function IntradayRefresher({ portfolioId }: { portfolioId: string }) {
  const router = useRouter();
  const [status, setStatus] = useState<IntradayStatus | null>(null);

  useEffect(() => {
    let alive = true;
    let id: ReturnType<typeof setInterval> | null = null;
    // First paint: fetch the current status immediately (the SSR'd page is already fresh,
    // so no refresh on this first call) — the label appears without waiting a full cycle.
    fetchIntradayStatusAction(portfolioId).then((s) => {
      if (!alive || !s) return;
      setStatus(s);
      // Persistent off-states won't change within the session — the user's intraday toggle
      // is off ("off") or this deployment has no feed ("feed") — so don't burn a 5-min
      // router.refresh cycle. Transient states (stale after the close, momentarily
      // unavailable) can recover during the session, so those keep polling.
      if (s.reason === "off" || s.reason === "feed") return;
      id = setInterval(async () => {
        const next = await fetchIntradayStatusAction(portfolioId);
        if (!alive) return;
        if (next) setStatus(next);
        router.refresh(); // re-render server components → NAV + positions revalue live
      }, REFRESH_MS);
    });
    return () => {
      alive = false;
      if (id) clearInterval(id);
    };
  }, [portfolioId, router]);

  if (!status || !status.applied) return null;

  return (
    <span className="text-[11px] text-muted" title="Position values + NAV recompute from delayed intraday quotes while open">
      intraday · ~15-min delayed{status.as_of_utc ? ` · as of ${asOf(status.as_of_utc)}` : ""}
    </span>
  );
}
