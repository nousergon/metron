"use client";

// Top / bottom holdings by return over a period (Day / YTD / LTM), ranked either by the
// holding's OWN price return or by its contribution to the portfolio return (weight ×
// return). Two horizontal bar lists side by side, computed entirely client-side from the
// holdings the Holdings page already loaded — instant toggles, no refetch, no chart dep.

import { useMemo, useState } from "react";
import type { Holding } from "@/lib/api";
import { accountingPercent, signClass } from "@/lib/format";

const PERIODS = [
  ["Day", "day_pct"],
  ["YTD", "ytd_pct"],
  ["LTM", "ltm_pct"],
] as const;
type PeriodKey = (typeof PERIODS)[number][1];

const BASES = [
  ["Price return", "price"],
  ["Contribution", "contrib"],
] as const;
type BasisKey = (typeof BASES)[number][1];

type Entry = { ticker: string; value: number; ret: number };

function ToggleGroup<T extends string>({
  options,
  value,
  onChange,
  label,
}: {
  options: ReadonlyArray<readonly [string, T]>;
  value: T;
  onChange: (v: T) => void;
  label: string;
}) {
  return (
    <div className="flex items-center gap-1" role="group" aria-label={label}>
      {options.map(([text, key]) => (
        <button
          key={key}
          type="button"
          onClick={() => onChange(key)}
          aria-pressed={key === value}
          className={`rounded-md border px-2 py-0.5 text-[11px] transition ${
            key === value ? "border-accent text-accent" : "border-line text-muted hover:border-muted"
          }`}
        >
          {text}
        </button>
      ))}
    </div>
  );
}

function BarList({ title, entries, maxAbs, basis }: { title: string; entries: Entry[]; maxAbs: number; basis: BasisKey }) {
  return (
    <div className="rounded-lg border border-line bg-surface p-4">
      <div className="mb-2 text-xs font-medium uppercase tracking-wide text-muted">{title}</div>
      {entries.length === 0 ? (
        <p className="text-xs text-muted">No holdings to rank.</p>
      ) : (
        <ul className="space-y-1.5">
          {entries.map((e) => {
            const pos = e.value >= 0;
            const width = maxAbs > 0 ? Math.max(2, (Math.abs(e.value) / maxAbs) * 100) : 0;
            return (
              <li key={e.ticker} className="flex items-center gap-2 text-xs">
                <span className="w-14 shrink-0 truncate font-medium" title={e.ticker}>
                  {e.ticker}
                </span>
                <div className="flex-1">
                  <div
                    className={`h-3 rounded-sm ${pos ? "bg-positive/70" : "bg-negative/70"}`}
                    style={{ width: `${width}%` }}
                  />
                </div>
                <span className={`w-20 shrink-0 text-right tabular-nums ${signClass(e.value)}`}>
                  {accountingPercent(e.value)}
                  {/* For the contribution basis the bar value is portfolio-return points; the
                      holding's own return is shown beneath so both are legible. */}
                  {basis === "contrib" ? (
                    <span className="ml-1 text-[10px] font-normal text-muted">({accountingPercent(e.ret)})</span>
                  ) : null}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

export function TopBottomPerformers({ holdings }: { holdings: Holding[] }) {
  const [period, setPeriod] = useState<PeriodKey>("ytd_pct");
  const [basis, setBasis] = useState<BasisKey>("price");

  const { top, bottom, maxAbs } = useMemo(() => {
    // Total base market value over priced holdings — the denominator for the weight in the
    // contribution basis. A holding with no base market value can't be weighted.
    const totalMv = holdings.reduce((s, h) => s + (h.market_value ?? 0), 0);
    const entries: Entry[] = [];
    for (const h of holdings) {
      const ret = h[period];
      if (ret == null) continue;
      if (basis === "contrib") {
        if (h.market_value == null || totalMv <= 0) continue;
        entries.push({ ticker: h.ticker, value: (h.market_value / totalMv) * ret, ret });
      } else {
        entries.push({ ticker: h.ticker, value: ret, ret });
      }
    }
    entries.sort((a, b) => b.value - a.value);
    // Disjoint top/bottom: up to 10 each on a large book; balanced halves on a small one so
    // the same holding never appears as both best and worst.
    const perSide = entries.length >= 20 ? 10 : Math.max(1, Math.floor(entries.length / 2));
    const top = entries.slice(0, perSide);
    const bottom = entries.slice(-perSide).reverse(); // worst first
    const maxAbs = entries.reduce((m, e) => Math.max(m, Math.abs(e.value)), 0);
    return { top, bottom, maxAbs };
  }, [holdings, period, basis]);

  const empty = top.length === 0 && bottom.length === 0;

  return (
    <div className="rounded-lg border border-line bg-surface/40 p-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <ToggleGroup options={PERIODS} value={period} onChange={setPeriod} label="Period" />
        <ToggleGroup options={BASES} value={basis} onChange={setBasis} label="Ranking basis" />
      </div>
      {empty ? (
        <p className="text-xs text-muted">
          No return data for this period{period === "day_pct" ? " (the intraday feed isn’t available)" : ""}.
        </p>
      ) : (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <BarList title="Top performers" entries={top} maxAbs={maxAbs} basis={basis} />
          <BarList title="Bottom performers" entries={bottom} maxAbs={maxAbs} basis={basis} />
        </div>
      )}
      <p className="mt-2 text-[11px] text-muted">
        {basis === "contrib"
          ? "Contribution = position weight × return (portfolio-return points); the holding’s own return is in parentheses."
          : "Each holding’s own price return over the period."}
      </p>
    </div>
  );
}
