// Compact macro snapshot at the top of the Overview (metron-ops#49, #64) — FRED is
// public-domain, so it stays in the no-feed beta. The standalone Macro page was retired
// (#64): these indicators live on the dashboard now.

import type { Macro, MacroIndicator } from "@/lib/api";
import { isoDate, signClass } from "@/lib/format";
import { Section } from "@/components/ui";

function value(ind: MacroIndicator): string {
  return ind.units === "%" ? `${ind.latest_value.toFixed(2)}%` : ind.latest_value.toFixed(2);
}

function change(ind: MacroIndicator): string {
  if (ind.change == null) return "—";
  const sign = ind.change > 0 ? "+" : ind.change < 0 ? "−" : "";
  const mag = Math.abs(ind.change).toFixed(2);
  return ind.units === "%" ? `${sign}${mag} pp` : `${sign}${mag}`;
}

export function MacroStrip({ macro }: { macro: Macro }) {
  if (!macro.available || macro.indicators.length === 0) return null;
  return (
    <Section title="Macro" note={macro.as_of ? `FRED · as of ${isoDate(macro.as_of)}` : "FRED"}>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {macro.indicators.slice(0, 8).map((ind) => (
          <div key={ind.key} className="rounded-lg border border-line p-3">
            <div className="truncate text-[10px] uppercase tracking-wide text-muted" title={ind.label}>
              {ind.label}
            </div>
            <div className="mt-0.5 text-lg font-semibold tabular-nums">{value(ind)}</div>
            <div className="flex items-baseline justify-between gap-1">
              <span className={`text-[11px] tabular-nums ${signClass(ind.change ?? 0)}`}>{change(ind)}</span>
              {/* Per-indicator freshness (metron-ops#49) — each series updates on its own cadence. */}
              <span className="text-[10px] tabular-nums text-muted" title={`Last updated ${isoDate(ind.latest_date)}`}>
                {isoDate(ind.latest_date)}
              </span>
            </div>
          </div>
        ))}
      </div>
    </Section>
  );
}
