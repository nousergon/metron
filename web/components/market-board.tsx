"use client";

// Market board (metron-ops-I304, Stage A): "Which stocks are technically attractive
// today?" across Held / Watchlist. Phone-first (390px) — a single scrollable table, no
// desktop-only chrome. Rates the SECURITY, never the holding (positioning `metron.md`
// §3d E8): no position-weight sort, no buy/sell CTA, no directive wording. A row tap goes
// to the existing tearsheet for that ticker.
//
// MEASURED 2026-09-14 (metron-ops#295 backfill grade): the rating carries NO predictive
// edge at 1-20 days (IC ~= -0.017 at 5d). The header states this plainly; nothing here is
// phrased as a recommendation.

import { useState, useTransition } from "react";
import Link from "next/link";
import type { MarketBoard, MarketBoardRow, MarketBoardScope } from "@/lib/api-market-board";
import { fetchMarketBoardAction } from "@/app/portfolios/[id]/market-board-action";
import { Empty, Section, Table } from "@/components/ui";
import { percent } from "@/lib/format";

const SCOPES: { key: MarketBoardScope; label: string }[] = [
  { key: "held", label: "Held" },
  { key: "watchlist", label: "Watchlist" },
];

function scoreTone(score: number | null): string {
  if (score == null) return "text-muted";
  if (score >= 0.2) return "text-positive";
  if (score <= -0.2) return "text-negative";
  return "text-muted";
}

function fmtChange(v: number | null): { text: string; cls: string } {
  if (v == null) return { text: "—", cls: "text-muted" };
  return { text: percent(v), cls: v > 0 ? "text-positive" : v < 0 ? "text-negative" : "text-muted" };
}

function asOfShort(iso: string | null): string {
  if (!iso) return "—";
  // Intraday as_of is a full ISO8601 UTC datetime; EOD as_of is a bare ISO date — both
  // start with the date, so a plain slice reads correctly for either.
  return iso.slice(0, 10);
}

function TrackRecordLine({ board }: { board: MarketBoard }) {
  const tr = board.track_record;
  return (
    <p className="mt-2 text-xs text-muted">
      Describes recent price action; graded IC ≈ 0 at 1–20 days (metron-ops#295). Not a forecast, not investment
      advice.
      {tr && tr.ic_mean != null ? (
        <>
          {" "}
          Measured: {tr.ic_mean.toFixed(3)} IC at {tr.horizon}d vs a {tr.noise_floor_ic != null ? tr.noise_floor_ic.toFixed(3) : "—"}{" "}
          noise floor ({tr.window} sessions, {tr.segment}).
        </>
      ) : null}
    </p>
  );
}

function Row({ portfolioId, row }: { portfolioId: string; row: MarketBoardRow }) {
  const day = fmtChange(row.change_1d_pct);
  const week = fmtChange(row.change_5d_pct);
  return (
    <tr className="border-b border-line last:border-0 hover:bg-white/5">
      <td className="p-0">
        <Link
          href={`/portfolios/${portfolioId}/tearsheet/${row.symbol}`}
          className="flex items-center gap-2 px-4 py-2 font-medium tabular-nums"
        >
          {row.symbol}
          {row.held ? (
            <span className="rounded bg-positive/10 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-positive">
              Held
            </span>
          ) : null}
        </Link>
      </td>
      <td className={`px-4 py-2 text-right tabular-nums ${scoreTone(row.score)}`}>
        {row.label ?? "—"}
        {row.score != null ? <span className="ml-1 text-muted">{row.score.toFixed(2)}</span> : null}
      </td>
      <td className="px-4 py-2 text-right tabular-nums text-muted">{row.ma_score != null ? row.ma_score.toFixed(2) : "—"}</td>
      <td className="px-4 py-2 text-right tabular-nums text-muted">{row.osc_score != null ? row.osc_score.toFixed(2) : "—"}</td>
      <td className={`px-4 py-2 text-right tabular-nums ${day.cls}`}>{day.text}</td>
      <td className={`px-4 py-2 text-right tabular-nums ${week.cls}`}>{week.text}</td>
      <td className="px-4 py-2 text-right text-muted">{row.basis ?? "—"}</td>
      <td className="px-4 py-2 text-right text-muted">{asOfShort(row.as_of)}</td>
    </tr>
  );
}

export function MarketBoardPanel({
  portfolioId,
  initialScope,
  initialBoard,
}: {
  portfolioId: string;
  initialScope: MarketBoardScope;
  initialBoard: MarketBoard;
}) {
  const [scope, setScope] = useState(initialScope);
  const [board, setBoard] = useState(initialBoard);
  const [error, setError] = useState<string | null>(null);
  const [pending, start] = useTransition();

  function selectScope(next: MarketBoardScope) {
    if (next === scope) return;
    setScope(next);
    setError(null);
    start(async () => {
      const r = await fetchMarketBoardAction(portfolioId, next);
      if (!r.ok) {
        setError(r.message);
        return;
      }
      setBoard(r.board);
    });
  }

  return (
    <div>
      <TrackRecordLine board={board} />

      <div className="mt-3 flex items-center gap-1.5">
        {SCOPES.map((s) => (
          <button
            key={s.key}
            type="button"
            onClick={() => selectScope(s.key)}
            aria-pressed={scope === s.key}
            disabled={pending}
            className={`rounded-full border px-3 py-1 text-xs transition disabled:opacity-50 ${
              scope === s.key ? "border-accent/40 bg-accent/10 text-ink" : "border-line text-muted hover:text-ink"
            }`}
          >
            {s.label}
          </button>
        ))}
      </div>

      {error ? <p className="mt-2 text-xs text-negative">{error}</p> : null}

      <Section title="Technical attractiveness" note={`${board.rows.length} ${board.rows.length === 1 ? "symbol" : "symbols"}`}>
        {board.rows.length === 0 ? (
          <Empty>
            {scope === "held"
              ? "No open positions to rate yet."
              : "Your watchlist is empty — add a ticker from the Watchlist page to see it here."}
          </Empty>
        ) : (
          <Table head={["Symbol", "Technical attractiveness", "MA", "Osc", "1d", "5d", "Basis", "As of"]}>
            {board.rows.map((row) => (
              <Row key={row.symbol} portfolioId={portfolioId} row={row} />
            ))}
          </Table>
        )}
      </Section>
    </div>
  );
}
