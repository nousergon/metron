"use client";

// Column-preset control for the Holdings table (metron-ops#114, #118+, realigned #140). The
// table carries ~40 columns across 10 bands; the only always-on (frozen) columns are Ticker +
// Market Value — everything else lives in a band the preset can toggle. SOTA pattern: a
// Ticker + Market Value spine + a swappable set of bands chosen by a preset. Every OTHER
// preset maps 1:1 onto its own named band — an analytic preset (Intraday / Valuation /
// Fundamentals / Attractiveness / Technicals / Consensus) never drags the Position/Value
// bands along for context; the frozen spine already anchors the row.
// Intraday leads the list and is the DEFAULT landing preset while the live valuation regime
// is resolved (market open, post-close recap, or frozen after hours — Brian, 2026-07-22):
// Last, Day $/%, the overnight/intraday decomposition, and Unrealized $/% in one
// self-sufficient band, so the page opens on "how are my stocks doing" without a click.
// Overview (Position + Value) is the settled-mode fallback — a straight rename would have
// misdescribed its Position half, which isn't intraday-relevant, so it stays a separate
// preset, just second in line now. Returns shows only the Returns band (Day/YTD/LTM) beside
// the spine, same as other analytic presets. "Customize" drops to band-level checkboxes for
// a bespoke set (→ "Custom").

import { BAND_ORDER, type ColumnBand } from "@/components/holdings-table";

export type PresetKey =
  | "intraday"
  | "overview"
  | "returns"
  | "valuation"
  | "fundamentals"
  | "attractiveness"
  | "technicals"
  | "consensus"
  | "classification"
  | "all";

export const COLUMN_PRESETS: { key: PresetKey; label: string; groups: ColumnBand[] }[] = [
  { key: "intraday", label: "Intraday", groups: ["Intraday"] },
  { key: "overview", label: "Overview", groups: ["Position", "Value"] },
  { key: "returns", label: "Returns", groups: ["Returns"] },
  { key: "valuation", label: "Valuation", groups: ["Valuation"] },
  { key: "fundamentals", label: "Fundamentals", groups: ["Fundamentals"] },
  { key: "attractiveness", label: "Attractiveness", groups: ["Attractiveness"] },
  { key: "technicals", label: "Technicals", groups: ["Technicals"] },
  { key: "consensus", label: "Consensus", groups: ["Consensus"] },
  { key: "classification", label: "Classify", groups: ["Class"] },
  { key: "all", label: "All", groups: [...BAND_ORDER] },
];

function bandsFor(key: PresetKey): ColumnBand[] {
  return COLUMN_PRESETS.find((p) => p.key === key)!.groups;
}

/** The settled-mode / no-live-available fallback — position economics (Position + Value)
 *  beside the Ticker spine. Looked up by key, not by array position — Intraday leads the
 *  preset list now (see header comment) but Overview remains this fallback. */
export const DEFAULT_VISIBLE_GROUPS: ColumnBand[] = bandsFor("overview");

/** The live-regime landing preset (see header comment) — Intraday's self-sufficient band. */
export const INTRADAY_VISIBLE_GROUPS: ColumnBand[] = bandsFor("intraday");

function sameBands(a: ColumnBand[], b: ColumnBand[]): boolean {
  if (a.length !== b.length) return false;
  const s = new Set(a);
  return b.every((g) => s.has(g));
}

const SEG_BTN = (active: boolean) =>
  `rounded-md px-2.5 py-1 transition ${active ? "bg-surface font-medium text-ink" : "text-muted hover:text-ink"}`;

export function ColumnPresetControl({
  value,
  onChange,
}: {
  value: ColumnBand[];
  onChange: (groups: ColumnBand[]) => void;
}) {
  const activeKey: PresetKey | "custom" =
    COLUMN_PRESETS.find((p) => sameBands(p.groups, value))?.key ?? "custom";

  // Always emit in canonical order so downstream rendering + the active-preset match are
  // order-insensitive.
  const emit = (groups: ColumnBand[]) => onChange(BAND_ORDER.filter((g) => groups.includes(g)));

  const toggleBand = (g: ColumnBand, on: boolean) =>
    emit(on ? [...value, g] : value.filter((x) => x !== g));

  return (
    <div className="flex flex-wrap items-center gap-2">
      <span className="text-[10px] uppercase tracking-wide text-muted">Columns</span>
      <div className="inline-flex flex-wrap rounded-lg border border-line p-0.5 text-xs">
        {COLUMN_PRESETS.map((p) => (
          <button
            key={p.key}
            type="button"
            onClick={() => emit(p.groups)}
            className={SEG_BTN(activeKey === p.key)}
            aria-pressed={activeKey === p.key}
          >
            {p.label}
          </button>
        ))}
      </div>
      {/* Native <details> disclosure — no click-outside wiring, accessible by default. */}
      <details className="relative text-xs">
        <summary
          className={`cursor-pointer list-none rounded-md border border-line px-2.5 py-1 ${
            activeKey === "custom" ? "bg-surface font-medium text-ink" : "text-muted hover:text-ink"
          }`}
        >
          Customize{activeKey === "custom" ? " ·" : ""}
        </summary>
        <div className="absolute right-0 z-30 mt-1 w-48 rounded-lg border border-line bg-paper p-2 shadow-lg">
          <p className="mb-1 px-1 text-[10px] uppercase tracking-wide text-muted">Column bands</p>
          {BAND_ORDER.map((g) => (
            <label key={g} className="flex cursor-pointer items-center gap-2 px-1 py-0.5 hover:text-ink">
              <input
                type="checkbox"
                checked={value.includes(g)}
                onChange={(e) => toggleBand(g, e.target.checked)}
              />
              {g}
            </label>
          ))}
        </div>
      </details>
    </div>
  );
}
