// Compact macro snapshot for the Overview (metron-ops#49) — Macro was buried on its own
// page; a few key indicators belong on the overview. FRED is public-domain, so it stays
// in the no-feed beta. Links out to the full Macro page.

import Link from "next/link";
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

export function MacroStrip({ macro, portfolioId }: { macro: Macro; portfolioId: string }) {
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
            <div className={`text-[11px] tabular-nums ${signClass(ind.change ?? 0)}`}>{change(ind)}</div>
          </div>
        ))}
      </div>
      <p className="mt-2 text-xs text-muted">
        <Link href={`/portfolios/${portfolioId}/macro`} className="text-accent hover:underline">
          Full macro →
        </Link>
      </p>
    </Section>
  );
}
