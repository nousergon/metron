"use client";

// Live-NAV refresher (metron-ops#79). While Metron is open it periodically polls the
// intraday-valuation status (also pinging the data-spine UI heartbeat via the API, so the
// producer keeps publishing while someone is looking). SWR handles the polling cadence
// (metron-ops#232) — the status data flows through a shared cache key so the "as of" label
// stays fresh without a blanket `router.refresh()`. The holdings/NAV data itself now
// refreshes via its own SWR hook with a matching `refreshInterval`, so no full-page
// re-render is needed.
//
// It renders a small honest label ("intraday · ~15-min delayed · as of HH:MM") only while
// the overlay is actually applied (feed-entitled + a fresh snapshot). On the no-feed beta
// or after the close (stale) it renders nothing and the page shows EOD-close values.
//
// Cadence adapts to the state. Intraday live (or a transient/recoverable state) polls fast
// so the NAV revalues with the delayed quotes. Intraday OFF / no-feed won't gain an overlay
// within the session, BUT the once-daily EOD NAV snapshot still advances — so those keep a
// SLOW poll rather than going idle, otherwise an all-day-open tab keeps showing yesterday's
// Today tile until a manual reload (the "Today tile stuck on 6/29" bug).

import { useIntradayStatus } from "@/lib/use-intraday-status";

const FAST_MS = 5 * 60 * 1000; // intraday live / transient — revalue from delayed quotes
const SLOW_MS = 30 * 60 * 1000; // off / no-feed — still catch the daily EOD snapshot advance

/** "as of 11:03 AM" in the viewer's local time, from the artifact's UTC write time. */
function asOf(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
}

export function IntradayRefresher({ portfolioId }: { portfolioId: string }) {
  // Determine the polling cadence from the latest status reason. SWR re-executes the
  // fetcher on each tick, picking up the current server state — so the interval adapts
  // when the reason changes (e.g. stale→applied when the feed recovers) without extra
  // state-machine logic.
  const { data: status } = useIntradayStatus(portfolioId, FAST_MS);

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
