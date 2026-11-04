"use client";

// The "markets" strip (Overview and the top of Holdings): intraday levels for the major
// indices (DIA/SPY/ONEQ/QQQ/IWM proxies for the Dow / S&P 500 / Nasdaq Composite /
// Nasdaq 100 / Russell 2000), auto-refreshed
// every ~5 min.
//
// Feed-gated (Pro): the server component renders this only when the `indices` feature is
// entitled (else a compact <Locked> / nothing). First paint uses the server-fetched
// `initial`; thereafter a 5-min poll of fetchIndicesAction keeps it live without a full
// page reload. The quotes are ~15-min delayed (the spine source) — surfaced honestly.

import { useEffect, useState } from "react";
import type { Indices } from "@/lib/api";
import { fetchIndicesAction } from "@/app/portfolios/[id]/markets-action";
import { percent, signClass } from "@/lib/format";
import { Section } from "@/components/ui";

const REFRESH_MS = 5 * 60 * 1000;

function level(n: number | null): string {
  return n != null ? n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : "—";
}

/** "as of 11:03 AM" in the viewer's local time, from the artifact's UTC write time. */
function asOf(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
}

export function IndexStrip({ initial }: { initial: Indices }) {
  const [data, setData] = useState<Indices>(initial);

  useEffect(() => {
    let alive = true;
    const id = setInterval(async () => {
      const next = await fetchIndicesAction();
      // Keep the last good snapshot on a transient failure / momentary unavailability.
      if (alive && next && next.available) setData(next);
    }, REFRESH_MS);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  if (!data.available || data.indices.length === 0) return null;

  const note = [
    "ETF proxies · ~15-min delayed",
    data.as_of_utc ? `as of ${asOf(data.as_of_utc)}${data.stale ? " (delayed)" : ""}` : null,
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <Section title="Markets" note={note}>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
        {data.indices.map((q) => (
          <div key={q.symbol} className="rounded-lg border border-line p-3">
            <div className="flex items-baseline justify-between gap-1">
              <span className="text-[10px] uppercase tracking-wide text-muted" title={`${q.symbol} — ${q.label}`}>
                {q.label}
              </span>
              <span className="text-[10px] tabular-nums text-muted/70">{q.symbol}</span>
            </div>
            <div className="mt-0.5 text-lg font-semibold tabular-nums">{level(q.last)}</div>
            {/* Today / MTD / YTD / LTM returns — TWR-comparable to the performance tiles below
                (metron-ops#87). Today = change vs prior close; YTD/LTM from cached closes. */}
            <div className="mt-1 grid grid-cols-4 gap-1 text-right">
              {([
                ["Today", q.change_pct],
                ["MTD", q.mtd_pct ?? null],
                ["YTD", q.ytd_pct],
                ["LTM", q.ltm_pct],
              ] as [string, number | null][]).map(([lbl, v], i) => (
                <div key={lbl}>
                  <div className="text-[9px] uppercase tracking-wide text-muted/70">{lbl}</div>
                  <div className={`text-[11px] tabular-nums ${v != null ? signClass(v) : "text-muted"}`}>
                    {v != null ? percent(v) : "—"}
                    {i === 0 && q.suspect ? (
                      <span className="ml-0.5 text-muted" title="Quote flagged suspect (possible bad scrape)">⚠</span>
                    ) : null}
                  </div>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
    </Section>
  );
}
