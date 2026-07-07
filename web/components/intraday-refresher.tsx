"use client";

// Live-NAV refresher (metron-ops#79). While Metron is open it periodically:
//   1. re-fetches the intraday-valuation status (also pinging the data-spine UI heartbeat
//      via the API, so the producer keeps publishing while someone is looking), and
//   2. calls router.refresh() so the server components re-render — the headline NAV, the
//      Today tile, and every position value pick up the freshest snapshot.
// It renders a small honest label ("intraday · ~15-min delayed · as of HH:MM") only while
// the overlay is actually applied (feed-entitled + a fresh snapshot). On the no-feed beta
// or after the close (stale) it renders nothing and the page shows EOD-close values.
//
// Cadence adapts to the state. Intraday live (or a transient/recoverable state) polls fast
// so the NAV revalues with the delayed quotes. Intraday OFF / no-feed won't gain an overlay
// within the session, BUT the once-daily EOD NAV snapshot still advances — so those keep a
// SLOW poll rather than going idle, otherwise an all-day-open tab keeps showing yesterday's
// Today tile until a manual reload (the "Today tile stuck on 6/29" bug).

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import type { IntradayStatus } from "@/lib/api";
import { fetchIntradayStatusAction } from "@/app/portfolios/[id]/intraday-action";

const FAST_MS = 5 * 60 * 1000; // intraday live / transient — revalue from delayed quotes
const SLOW_MS = 30 * 60 * 1000; // off / no-feed — still catch the daily EOD snapshot advance

/** How often to refresh given the current intraday reason. "off" (toggle off) and "feed"
 *  (deployment has no feed) won't gain an overlay this session, but the daily EOD snapshot
 *  still moves, so they poll slowly rather than not at all. Everything else polls fast. */
function intervalFor(reason: string | null | undefined): number {
  return reason === "off" || reason === "feed" ? SLOW_MS : FAST_MS;
}

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
    let timer: ReturnType<typeof setTimeout> | null = null;

    // Self-scheduling poll: each tick re-reads the status (so the cadence adapts when the
    // reason changes — e.g. stale→applied when the feed recovers) and re-renders the server
    // components so NAV / Today tile / positions pick up the freshest snapshot.
    const schedule = (reason: string | null | undefined) => {
      if (!alive) return;
      timer = setTimeout(async () => {
        if (!alive) return;
        const next = await fetchIntradayStatusAction(portfolioId);
        if (!alive) return;
        if (next) setStatus(next);
        router.refresh();
        schedule(next?.reason);
      }, intervalFor(reason));
    };

    // First paint: fetch the current status immediately (the SSR'd page is already fresh,
    // so no refresh on this first call) — the label appears without waiting a full cycle —
    // then start the poll at the cadence this state warrants.
    fetchIntradayStatusAction(portfolioId).then((s) => {
      if (!alive) return;
      if (s) setStatus(s);
      schedule(s?.reason);
    });
    return () => {
      alive = false;
      if (timer) clearTimeout(timer);
    };
  }, [portfolioId, router]);

  if (!status || !status.applied) return null;

  // Coverage disclosure (metron-ops#146/#152): a symbol without a usable quote keeps its
  // EOD close inside the same NAV, so a partial overlay must say so — NAV-WEIGHTED (a
  // ticker count misstates coverage whenever position sizes differ). Full coverage (the
  // norm) stays quiet; the count remains in the hover title.
  const partial = status.n_total > 0 && status.n_priced < status.n_total;
  const navPct =
    status.covered_nav != null && status.total_nav
      ? Math.round((status.covered_nav / status.total_nav) * 100)
      : null;

  return (
    <span
      className="text-[11px] text-muted"
      title={`Position values + NAV recompute from delayed intraday quotes while open; positions without a usable quote stay at their last close${partial ? ` (${status.n_priced}/${status.n_total} holdings live)` : ""}`}
    >
      intraday · ~15-min delayed{status.as_of_utc ? ` · as of ${asOf(status.as_of_utc)}` : ""}
      {partial ? (navPct != null ? ` · covers ${navPct}% of NAV` : ` · ${status.n_priced}/${status.n_total} live`) : ""}
    </span>
  );
}
