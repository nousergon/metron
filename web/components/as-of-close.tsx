// Settled-view freshness badge (metron-ops#145/#146). Tabs outside the LIVE pair
// (Overview / Holdings) value everything from last-trading-day close data; this badge is
// the one place that says so, sourced from the API's own as-of metadata — static page copy
// never makes freshness claims. Counterpart of the live label in intraday-refresher.tsx.

import { isoDate } from "@/lib/format";

export function AsOfClose({ date, prefix = "as of" }: { date: string | null | undefined; prefix?: string }) {
  if (!date) return null;
  return (
    <span
      className="text-[11px] text-muted"
      title="Settled view — computed from market-close data through this date. Intraday moves are not reflected here; Overview and Holdings carry the live values."
    >
      {prefix} {isoDate(date)} close
    </span>
  );
}
