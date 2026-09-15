"use client";

// The targets editor (metron-ops-I311) — the user's own per-security list, weight caps,
// and minimum line. NOTHING here is pre-filled or suggested: a portfolio with no saved
// targets renders every row empty (intelligence-doctrine layer 2 — "the user types the
// number; Metron does not choose securities"). Saving replaces the whole list verbatim.

import { useState, useTransition } from "react";
import type { PlanTargets, TargetLine } from "@/lib/api-planning";
import { savePlanTargetsAction } from "@/app/portfolios/[id]/planning-actions";
import { pct1 } from "@/lib/format";
import { Section } from "@/components/ui";

type Row = { symbol: string; weight: string };

function toRows(targets: TargetLine[]): Row[] {
  return targets.map((t) => ({ symbol: t.symbol, weight: String(t.weight * 100) }));
}

export function PlanTargetsForm({ portfolioId, initial }: { portfolioId: string; initial: PlanTargets }) {
  // Empty rows by default (or exactly what was already saved) — never a suggested
  // starting value. One blank row is offered as a place to type, not a filled example.
  const [rows, setRows] = useState<Row[]>(initial.targets.length > 0 ? toRows(initial.targets) : [{ symbol: "", weight: "" }]);
  const [cap, setCap] = useState<string>(initial.max_single_position != null ? String(initial.max_single_position * 100) : "");
  const [minLine, setMinLine] = useState<string>(initial.min_line_usd != null ? String(initial.min_line_usd) : "");
  const [saved, setSaved] = useState<PlanTargets | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();

  function updateRow(i: number, patch: Partial<Row>) {
    setRows((prev) => prev.map((r, idx) => (idx === i ? { ...r, ...patch } : r)));
  }
  function addRow() {
    setRows((prev) => [...prev, { symbol: "", weight: "" }]);
  }
  function removeRow(i: number) {
    setRows((prev) => prev.filter((_, idx) => idx !== i));
  }

  function submit(e: React.FormEvent) {
    e.preventDefault();
    const targets = rows
      .map((r) => ({ symbol: r.symbol.trim().toUpperCase(), weight: Number(r.weight) / 100 }))
      .filter((t) => t.symbol && Number.isFinite(t.weight) && t.weight > 0);
    const capFrac = cap.trim() ? Number(cap) / 100 : null;
    const minLineUsd = minLine.trim() ? Number(minLine) : null;
    startTransition(async () => {
      const res = await savePlanTargetsAction(portfolioId, {
        targets,
        max_single_position: capFrac,
        min_line_usd: minLineUsd,
      });
      if (res.ok) {
        setSaved(res.targets);
        setError(null);
      } else {
        setSaved(null);
        setError(res.message);
      }
    });
  }

  const totalPct = rows.reduce((acc, r) => acc + (Number(r.weight) || 0), 0);

  return (
    <Section title="Your targets" note={saved ? "saved" : undefined}>
      <form onSubmit={submit} className="rounded-lg border border-line p-4">
        <p className="text-xs text-muted">
          Type the securities and weights you want. Arithmetic against the targets you set — Metron does not choose
          securities.
        </p>

        <div className="mt-3 flex flex-col gap-2">
          {rows.map((row, i) => (
            <div key={i} className="flex flex-wrap items-center gap-2">
              <input
                type="text"
                value={row.symbol}
                onChange={(e) => updateRow(i, { symbol: e.target.value })}
                placeholder="Ticker"
                aria-label={`Target ${i + 1} ticker`}
                className="w-28 rounded border border-line bg-paper px-2 py-1 text-sm uppercase"
              />
              <input
                type="number"
                inputMode="decimal"
                min={0}
                max={100}
                step={0.5}
                value={row.weight}
                onChange={(e) => updateRow(i, { weight: e.target.value })}
                placeholder="Weight %"
                aria-label={`Target ${i + 1} weight percent`}
                className="w-24 rounded border border-line bg-paper px-2 py-1 text-right text-sm tabular-nums"
              />
              <button
                type="button"
                onClick={() => removeRow(i)}
                aria-label={`Remove target ${i + 1}`}
                className="text-xs text-muted hover:text-ink"
              >
                Remove
              </button>
            </div>
          ))}
        </div>

        <button
          type="button"
          onClick={addRow}
          className="mt-2 rounded border border-line px-2 py-1 text-xs hover:bg-white/5"
        >
          + Add a target
        </button>

        <div className="mt-4 flex flex-wrap items-end gap-3">
          <label className="text-sm">
            <span className="block text-xs uppercase tracking-wide text-muted">Max per position (optional)</span>
            <input
              type="number"
              inputMode="decimal"
              min={0}
              max={100}
              step={0.5}
              value={cap}
              onChange={(e) => setCap(e.target.value)}
              placeholder="No cap"
              aria-label="Max per position percent"
              className="mt-1 w-28 rounded border border-line bg-paper px-2 py-1 text-right text-sm tabular-nums"
            />
          </label>
          <label className="text-sm">
            <span className="block text-xs uppercase tracking-wide text-muted">Minimum line ($, optional)</span>
            <input
              type="number"
              inputMode="decimal"
              min={0}
              step={50}
              value={minLine}
              onChange={(e) => setMinLine(e.target.value)}
              placeholder="No minimum"
              aria-label="Minimum line size in dollars"
              className="mt-1 w-32 rounded border border-line bg-paper px-2 py-1 text-right text-sm tabular-nums"
            />
          </label>
          <button
            type="submit"
            disabled={pending}
            className="rounded border border-line px-3 py-1.5 text-sm transition hover:border-muted hover:bg-white/5 disabled:opacity-50"
          >
            {pending ? "Saving…" : "Save targets"}
          </button>
          <span className="text-xs text-muted">Total {pct1(totalPct / 100)}</span>
        </div>

        {error ? <p className="mt-3 text-sm text-muted">{error}</p> : null}
      </form>
    </Section>
  );
}
