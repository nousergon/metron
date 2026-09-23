// Tearsheet "Factor score" gauge (metron-ops#106, Phase 2) — a transparent 0–100 blend; the
// breakdown surfaces each pillar's weight + sub-score so it's never a black box. Feed-gated.
//
// Three states, kept visually distinct (metron-ops-I334):
//   - retired  → the section stays, reading "Retired" (v1 factor substrate discontinued);
//   - stale    → the gauge renders with an explicit "stale" tag beside the number;
//   - no score → nothing (off-feed or this ticker is outside the scanner universe — an honest
//                coverage gap, never presented as retired or stale).

import type { TearsheetAttractiveness } from "@/lib/api";
import { isoDate, percent } from "@/lib/format";
import { Section, Table } from "@/components/ui";
import { FACTOR_RETIRED_TITLE, FactorStaleTag, factorScoreState } from "@/components/factor-score-state";

// Pillar keys → human labels. The count is the catalog size — the note reads "N of M inputs".
export const ATTRACTIVENESS_COMPONENT_LABELS: Record<string, string> = {
  quality: "Quality",
  value: "Value",
  momentum: "Momentum",
  growth: "Growth",
  stewardship: "Stewardship",
  defensiveness: "Defensiveness",
};
const COMPONENT_LABELS_COUNT = Object.keys(ATTRACTIVENESS_COMPONENT_LABELS).length;

export function TearsheetFactorScore({ attractiveness: a }: { attractiveness: TearsheetAttractiveness }) {
  const state = factorScoreState({ stale: a.stale, retired: a.retired });
  if (state === "retired") {
    return (
      <Section title="Factor score" note="retired">
        <div className="rounded-lg border border-dashed border-line p-6 text-sm text-muted" data-factor-state="retired">
          {FACTOR_RETIRED_TITLE}
        </div>
      </Section>
    );
  }
  if (!a.available || a.score == null) return null;

  const score = a.score;
  const stale = state === "stale";
  const scoreTone = score >= 60 ? "text-positive" : score <= 40 ? "text-negative" : "";
  const barTone = score >= 60 ? "bg-positive" : score <= 40 ? "bg-negative" : "bg-muted";
  return (
    <Section
      title="Factor score"
      note={`composite · ${a.coverage ?? 0} of ${COMPONENT_LABELS_COUNT} inputs${a.as_of ? ` · as of ${isoDate(a.as_of)}` : ""}`}
    >
      <div className="flex items-center gap-4">
        <div className={`text-3xl font-semibold tabular-nums ${scoreTone}`}>
          {score.toFixed(1)}
          <span className="ml-1 text-sm text-muted">/ 100</span>
          {stale ? <FactorStaleTag /> : null}
        </div>
        <div className="h-2 flex-1 overflow-hidden rounded-full bg-line">
          <div className={`h-full ${barTone}`} style={{ width: `${Math.max(0, Math.min(100, score))}%` }} />
        </div>
      </div>
      {/* Inspectable weighting — the deliberate "not a black box" deliverable. */}
      <div className="mt-4">
        <Table head={["Pillar", "Weight", "Score"]}>
          {a.components.map((c) => (
            <tr key={c.key} className="border-b border-line last:border-0">
              <td className="px-4 py-2">{ATTRACTIVENESS_COMPONENT_LABELS[c.key] ?? c.key}</td>
              <td className="px-4 py-2 text-right tabular-nums">{percent(c.weight)}</td>
              <td className="px-4 py-2 text-right tabular-nums">{c.score.toFixed(0)}</td>
            </tr>
          ))}
        </Table>
      </div>
    </Section>
  );
}
