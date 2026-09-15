// Per-row provenance badge for the glance screen (positioning §3g.4 law 4): every number
// says what it is — live (delayed quote), settled, as of the broker sync, or as of close.

import type { GlanceProvenance } from "@/lib/api-glance";
import { isoDate } from "@/lib/format";

const LABEL: Record<GlanceProvenance, string> = {
  live: "live",
  settled: "settled",
  as_of_sync: "as of sync",
  as_of_close: "as of close",
};

/** A date-only as-of renders as the date; a timestamp renders as ET wall time. */
export function formatAsOf(asOf: string): string {
  if (!asOf.includes("T")) return isoDate(asOf);
  const d = new Date(asOf);
  if (Number.isNaN(d.getTime())) return asOf;
  return `${d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", timeZone: "America/New_York" })} ET`;
}

export function ProvenanceBadge({
  provenance,
  asOf,
}: {
  provenance: GlanceProvenance | null | undefined;
  asOf: string | null | undefined;
}) {
  if (!provenance || !asOf) return null;
  return (
    <span
      data-provenance={provenance}
      className={`whitespace-nowrap rounded border px-1.5 py-0.5 text-[10px] uppercase tracking-wider ${
        provenance === "live" ? "border-accent/50 text-accent" : "border-line text-muted"
      }`}
    >
      {LABEL[provenance]} · {formatAsOf(asOf)}
    </span>
  );
}
