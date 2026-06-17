// NAV bridge (metron-ops#60) — decomposes how the latest NAV was built: the value when
// recorded history began, the net contributions (buys − sells) added since, and the
// investment gain that's left over. Latest NAV = start + contributions + gain.

import { moneyWhole, signClass, signedMoneyWhole } from "@/lib/format";

export function NavBridge({
  start,
  contributions,
  gain,
  end,
  currency,
}: {
  start: number;
  contributions: number;
  gain: number;
  end: number;
  currency: string;
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
    </div>
  );
}
