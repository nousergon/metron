"use client";

// The Overview hero (metron-ops#83): aggregate holdings performance vs the market over
// Today / MTD / YTD / LTM. Each tile shows the $ investment gain, the %TWR (flow-neutralized),
// and — per user-toggled benchmark — the alpha (portfolio %TWR − index %).
//
// Benchmark comparison is feed-gated (Pro): in the no-feed beta the server passes
// `benchmarksAvailable=false` and the tiles render portfolio-only (no toggles, no alpha
// rows). The benchmark toggles are client state, so this is a client component; the data
// itself is server-fetched and passed in.

import { useState } from "react";
import Link from "next/link";
import type { BenchmarkReturn, PeriodTile, TileDay } from "@/lib/api";
import { isoDate, moneyWhole, percent, signClass, signedMoneyWhole } from "@/lib/format";

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

function DayLine({ d }: { d: TileDay }) {
  return (
    <li className="tabular-nums">
      {isoDate(d.when)}: {moneyWhole(d.prev_nav)} → {moneyWhole(d.nav)}
      {d.flow ? `, net purchases ${signedMoneyWhole(d.flow)}` : ""} ={" "}
      <span className={signClass(d.ret)}>{percent(d.ret)}</span>
    </li>
  );
}

/** "How this is computed" (Brian 2026-11-04): every figure on the tile, from its inputs. */
function TileMath({ tile }: { tile: PeriodTile }) {
  const b = tile.basis;
  if (!b || !tile.start_date || !tile.end_date) return null;
  const priced = tile.benchmarks.filter((x) => x.ret != null && x.start_close != null && x.end_close != null);
  return (
    <details className="mt-3 border-t border-line pt-2 text-[11px] leading-relaxed text-muted">
      <summary className="cursor-pointer select-none hover:text-ink">How this is computed</summary>
      <div className="mt-1 space-y-1.5">
        <p className="tabular-nums">
          Holdings value {moneyWhole(b.start_nav)} on {isoDate(tile.start_date)}, {moneyWhole(b.end_nav)} on{" "}
          {isoDate(tile.end_date)}. Net purchases in between {signedMoneyWhole(b.net_flows)}. Gain = end − start −
          purchases = {tile.gain != null ? signedMoneyWhole(tile.gain) : "—"}.
        </p>
        <p>
          TWR chains {b.steps} snapshot-to-snapshot returns, each with that day&apos;s purchases taken out, so money
          added is never counted as return.
          {b.estimated_points > 0 ? ` ${b.estimated_points} of the snapshots used an estimated input.` : ""}
        </p>
        {b.suspect_days.length > 0 ? (
          <div className="text-amber-600 dark:text-amber-400">
            Check these days. A one-step move over 8% is usually a missing deposit, sale or transfer, not the market:
            <ul className="ml-3 list-disc">
              {b.suspect_days.map((d) => (
                <DayLine key={d.when} d={d} />
              ))}
            </ul>
          </div>
        ) : b.largest_days.length > 0 ? (
          <div>
            Biggest days:
            <ul className="ml-3 list-disc">
              {b.largest_days.map((d) => (
                <DayLine key={d.when} d={d} />
              ))}
            </ul>
          </div>
        ) : null}
        {priced.length > 0 ? (
          <div>
            Index returns, ETF close to close:
            <ul className="ml-3 list-disc tabular-nums">
              {priced.map((x) => (
                <li key={x.symbol}>
                  {x.label} ({x.symbol}) {x.start_close!.toFixed(2)} → {x.end_close!.toFixed(2)} = {percent(x.ret!)}
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </div>
    </details>
  );
}

function Tile({ tile, selected, sessionHref }: { tile: PeriodTile; selected: Set<string>; sessionHref?: string }) {
  const has = tile.twr != null || tile.mwr != null || tile.gain != null;
  const shown = tile.benchmarks.filter((b) => selected.has(b.symbol));
  // Only MTD/YTD/LTM carry a meaningful span worth labeling; Today is self-evident.
  const span = tile.period !== "today" && tile.start_date ? isoDate(tile.start_date) : null;

  return (
    <div className="rounded-lg border border-line p-4">
      <div className="flex items-baseline justify-between gap-2">
        <div className="text-xs font-medium uppercase tracking-wide text-muted">{tile.label}</div>
        <div className="flex items-baseline gap-2">
          {tile.note ? (
            // TODAY is settled by construction (metron-ops#154) — a window ending before the
            // current session carries "as of <date>" so a completed-session change is never
            // read as a live "today" move.
            <div className="text-[10px] text-muted/70">{tile.note}</div>
          ) : span ? (
            <div className="text-[10px] text-muted/70">since {span}</div>
          ) : null}
          {tile.period === "today" && sessionHref ? (
            <Link
              href={sessionHref}
              className="text-[10px] text-accent/80 transition hover:text-accent"
              title="Live session detail — Holdings in the live valuation mode"
            >
              live →
            </Link>
          ) : null}
        </div>
      </div>
      {has ? (
        <>
          <div className={`mt-2 text-2xl font-semibold tabular-nums ${signClass(tile.gain ?? 0)}`}>
            {tile.gain != null ? signedMoneyWhole(tile.gain) : "—"}
          </div>
          <div className="mt-0.5 flex items-baseline gap-3">
            <div className={`text-sm tabular-nums ${signClass(tile.twr ?? 0)}`}>
              {tile.twr != null ? `${percent(tile.twr)} TWR` : "—"}
            </div>
            <div className={`text-sm tabular-nums ${signClass(tile.mwr ?? 0)}`}>
              {tile.mwr != null ? `${percent(tile.mwr)} MWR` : "—"}
            </div>
          </div>
          {shown.length > 0 ? (
            <div className="mt-3 space-y-1 border-t border-line pt-2">
              {shown.map((b) => (
                <AlphaRow key={b.symbol} b={b} />
              ))}
            </div>
          ) : null}
          <TileMath tile={tile} />
        </>
      ) : (
        <div className="mt-2 text-sm text-muted">{tile.note ?? "history building…"}</div>
      )}
    </div>
  );
}

export function PerfTiles({
  tiles,
  benchmarksAvailable,
  sessionHref,
}: {
  tiles: PeriodTile[];
  benchmarksAvailable: boolean;
  /** Link into the Holdings live valuation mode, shown on the (settled) Today tile. */
  sessionHref?: string;
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
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {tiles.map((t) => (
          <Tile key={t.period} tile={t} selected={selected} sessionHref={sessionHref} />
        ))}
      </div>
    </div>
  );
}
