"use client";

// The Overview hero (metron-ops#83): aggregate holdings performance vs the market over
// Today / YTD / LTM. Each tile shows the $ investment gain, the %TWR (flow-neutralized),
// and — per user-toggled benchmark — the alpha (portfolio %TWR − index %).
//
// Benchmark comparison is feed-gated (Pro): in the no-feed beta the server passes
// `benchmarksAvailable=false` and the tiles render portfolio-only (no toggles, no alpha
// rows). The benchmark toggles are client state, so this is a client component; the data
// itself is server-fetched and passed in.

import { useState } from "react";
import type { BenchmarkReturn, PeriodTile } from "@/lib/api";
import { isoDate, percent, signClass, signedMoneyWhole } from "@/lib/format";

/** The benchmark universe across the tiles (same set on each) → [{symbol, label}]. */
function benchmarkMeta(tiles: PeriodTile[]): { symbol: string; label: string }[] {
  const seen = new Map<string, string>();
  for (const t of tiles) for (const b of t.benchmarks) if (!seen.has(b.symbol)) seen.set(b.symbol, b.label);
  return [...seen].map(([symbol, label]) => ({ symbol, label }));
}

function AlphaRow({ b }: { b: BenchmarkReturn }) {
  return (
    <div className="flex items-baseline justify-between gap-2 text-xs">
      <span className="text-muted">vs {b.label}</span>
      <span className={`tabular-nums ${b.alpha != null ? signClass(b.alpha) : "text-muted"}`}>
        {b.alpha != null ? percent(b.alpha) : "—"}
      </span>
    </div>
  );
}

function Tile({ tile, selected }: { tile: PeriodTile; selected: Set<string> }) {
  const has = tile.twr != null || tile.gain != null;
  const shown = tile.benchmarks.filter((b) => selected.has(b.symbol));
  // Only YTD/LTM carry a meaningful span worth labeling; Today is self-evident.
  const span = tile.period !== "today" && tile.start_date ? isoDate(tile.start_date) : null;

  return (
    <div className="rounded-lg border border-line p-4">
      <div className="flex items-baseline justify-between">
        <div className="text-xs font-medium uppercase tracking-wide text-muted">{tile.label}</div>
        {span ? <div className="text-[10px] text-muted/70">since {span}</div> : null}
      </div>
      {has ? (
        <>
          <div className={`mt-2 text-2xl font-semibold tabular-nums ${signClass(tile.gain ?? 0)}`}>
            {tile.gain != null ? signedMoneyWhole(tile.gain) : "—"}
          </div>
          <div className={`mt-0.5 text-sm tabular-nums ${signClass(tile.twr ?? 0)}`}>
            {tile.twr != null ? `${percent(tile.twr)} TWR` : "—"}
          </div>
          {shown.length > 0 ? (
            <div className="mt-3 space-y-1 border-t border-line pt-2">
              {shown.map((b) => (
                <AlphaRow key={b.symbol} b={b} />
              ))}
            </div>
          ) : null}
        </>
      ) : (
        <div className="mt-2 text-sm text-muted">history building…</div>
      )}
    </div>
  );
}

export function PerfTiles({
  tiles,
  benchmarksAvailable,
}: {
  tiles: PeriodTile[];
  benchmarksAvailable: boolean;
}) {
  const meta = benchmarksAvailable ? benchmarkMeta(tiles) : [];
  const [selected, setSelected] = useState<Set<string>>(() => new Set(meta.map((m) => m.symbol)));

  const toggle = (symbol: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(symbol)) next.delete(symbol);
      else next.add(symbol);
      return next;
    });

  return (
    <div className="mt-6">
      <div className="mb-2 flex items-center justify-between">
        <div className="text-xs uppercase tracking-wide text-muted">Performance vs market</div>
        {meta.length > 0 ? (
          <div className="flex gap-1.5">
            {meta.map((m) => {
              const on = selected.has(m.symbol);
              return (
                <button
                  key={m.symbol}
                  type="button"
                  onClick={() => toggle(m.symbol)}
                  aria-pressed={on}
                  className={`rounded-full border px-2 py-0.5 text-[11px] transition ${
                    on ? "border-accent text-accent" : "border-line text-muted hover:border-muted"
                  }`}
                >
                  {m.symbol}
                </button>
              );
            })}
          </div>
        ) : (
          <span className="text-[11px] text-muted/70">Benchmarks: Pro</span>
        )}
      </div>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        {tiles.map((t) => (
          <Tile key={t.period} tile={t} selected={selected} />
        ))}
      </div>
    </div>
  );
}
