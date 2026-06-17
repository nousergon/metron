// NAV bridge (metron-ops#60) — decomposes how the latest NAV was built: the value when
// recorded history began, the net contributions (buys − sells) added since, and the
// investment gain that's left over. Latest NAV = start + contributions + gain.

import { moneyWhole, signClass, signedMoneyWhole } from "@/lib/format";

export type YearBridge = { year: number; start: number; contributions: number; gain: number; end: number };

export function NavBridge({
  start,
  contributions,
  gain,
  end,
  currency,
  years,
}: {
  start: number;
  contributions: number;
  gain: number;
  end: number;
  currency: string;
  /** Optional per-calendar-year breakdown (metron-ops#68) — rendered as a small table
   *  under the whole-history bridge when more than one year is present. */
  years?: YearBridge[];
}) {
  const total = Math.max(end, 1);
  const pct = (v: number) => `${Math.min(100, Math.max(0, (v / total) * 100))}%`;

  return (
    <div className="rounded-lg border border-line bg-surface p-4">
      <div className="mb-3 text-xs font-medium uppercase tracking-wide text-muted">How your NAV was built</div>

      {/* Proportion bar: start + contributions + gain → latest NAV (only when gain ≥ 0,
          so a loss never renders a negative-width segment). */}
      {gain >= 0 ? (
        <div className="flex h-2 overflow-hidden rounded-full">
          <div style={{ width: pct(start) }} className="bg-muted/40" title="Starting value" />
          <div style={{ width: pct(contributions) }} className="bg-accent/50" title="Net contributions" />
          <div style={{ width: pct(gain) }} className="bg-positive/60" title="Investment gain" />
        </div>
      ) : null}

      <div className="mt-3 grid grid-cols-2 gap-3 text-sm tabular-nums sm:grid-cols-4">
        <div>
          <div className="text-[10px] uppercase tracking-wide text-muted">Starting value</div>
          <div>{moneyWhole(start, currency)}</div>
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-wide text-muted">+ Net contributions</div>
          <div>{signedMoneyWhole(contributions, currency)}</div>
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-wide text-muted">+ Investment gain</div>
          <div className={signClass(gain)}>{signedMoneyWhole(gain, currency)}</div>
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-wide text-muted">= Latest NAV</div>
          <div className="font-semibold">{moneyWhole(end, currency)}</div>
        </div>
      </div>

      <p className="mt-2 text-[11px] text-muted">
        Latest NAV = value when history began + net contributions (buys − sells) + investment gain.
      </p>

      {years && years.length > 1 ? (
        <div className="mt-4 overflow-x-auto rounded-md border border-line">
          <table className="w-full text-sm tabular-nums">
            <thead>
              <tr className="border-b border-line bg-surface text-left text-[10px] uppercase tracking-wide text-muted">
                <th className="px-3 py-1.5 font-medium">Year</th>
                <th className="px-3 py-1.5 text-right font-medium">Start</th>
                <th className="px-3 py-1.5 text-right font-medium">+ Contributions</th>
                <th className="px-3 py-1.5 text-right font-medium">+ Gain</th>
                <th className="px-3 py-1.5 text-right font-medium">= End</th>
              </tr>
            </thead>
            <tbody>
              {years.map((y) => (
                <tr key={y.year} className="border-b border-line last:border-0">
                  <td className="px-3 py-1.5 font-medium">{y.year}</td>
                  <td className="px-3 py-1.5 text-right text-muted">{moneyWhole(y.start, currency)}</td>
                  <td className="px-3 py-1.5 text-right">{signedMoneyWhole(y.contributions, currency)}</td>
                  <td className={`px-3 py-1.5 text-right ${signClass(y.gain)}`}>{signedMoneyWhole(y.gain, currency)}</td>
                  <td className="px-3 py-1.5 text-right font-medium">{moneyWhole(y.end, currency)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </div>
  );
}
