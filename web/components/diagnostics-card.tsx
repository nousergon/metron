// Concentration & diversification diagnostics card (metron-ops-I167) — the
// Intelligence lane's deterministic surface (metron-ops-I164). Everything rendered
// here is a FACT: concentration measurements, benchmark-relative sector weights, the
// geography split, and a MECHANICAL evaluation of the user's own stated targets
// ("you set X; actual is Y"). No generated copy anywhere — the explainers are static
// definitions, and freshness comes from the API's as-of metadata, never page copy.

import type { Diagnostics, TargetDriftRow } from "@/lib/api";
import { accountingPercent, moneyWhole, percent } from "@/lib/format";
import { Section, StatCard, Table } from "@/components/ui";

function pct(ratio: number | null): string {
  return ratio != null ? accountingPercent(ratio) : "—";
}

/** Signed over/underweight delta vs the benchmark. */
function deltaPct(delta: number | null): string {
  return delta != null ? percent(delta) : "—";
}

/** Static, plain-language definitions for the concentration metrics — fixed copy, not
 *  generated, and descriptive only (what the number measures, never what to do). */
function ExplainerLegend() {
  return (
    <div className="mt-3 rounded-lg border border-line bg-surface p-4 text-xs text-muted">
      <div className="mb-1 font-medium uppercase tracking-wide">What these measure</div>
      <ul className="list-disc space-y-1 pl-4">
        <li>
          <span className="text-ink">HHI</span> (Herfindahl–Hirschman index) — the sum of squared position weights.
          Ranges from near 0 (many small positions) to 1 (a single position); an equal-weight portfolio of N
          positions scores 1/N.
        </li>
        <li>
          <span className="text-ink">Effective positions</span> — 1 ÷ HHI: the number of equal-weight positions with
          the same concentration as this portfolio.
        </li>
        <li>
          <span className="text-ink">Top-5 / top-10 share</span> — the fraction of included market value held in the
          5 (10) largest positions.
        </li>
        <li>
          <span className="text-ink">Weights</span> are shares of priced, non-cash settled market value. Holdings the
          reference data can’t classify appear in an explicit “Unclassified” bucket — never assigned a guessed sector
          or country.
        </li>
      </ul>
    </div>
  );
}

/** The target-drift status column: the mechanical outcome of the user's own rule. */
function driftStatus(row: TargetDriftRow): { text: string; className: string } {
  if (row.actual == null) return { text: `not measurable${row.detail ? ` — ${row.detail}` : ""}`, className: "text-muted" };
  if (row.kind === "max_position") {
    return row.breach
      ? { text: "above your stated max", className: "text-negative" }
      : { text: "within your stated max", className: "text-muted" };
  }
  if (row.kind === "avoid_sector") {
    return row.breach
      ? { text: `held${row.detail ? `: ${row.detail}` : ""}`, className: "text-negative" }
      : { text: "not held", className: "text-muted" };
  }
  // Pure drift (allocation): report the signed gap, no rule verdict.
  return row.target != null
    ? { text: `${percent(row.actual - row.target)} vs target`, className: "text-muted" }
    : { text: "", className: "text-muted" };
}

function driftRuleLabel(row: TargetDriftRow): string {
  if (row.kind === "max_position") return `Max single position — ${row.label}`;
  if (row.kind === "avoid_sector") return `Avoid sector — ${row.label}`;
  return `Allocation — ${row.label}`;
}

export function DiagnosticsCard({ d }: { d: Diagnostics }) {
  const c = d.concentration;
  const geoTotal = d.total_market_value;
  const unclassifiedSector = d.sectors.find((s) => s.sector === "Unclassified");
  return (
    <>
      {c ? (
        <>
          <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-5">
            <StatCard label="Positions" value={String(c.n_positions)} hint="priced, non-cash" />
            <StatCard label="HHI" value={c.hhi.toFixed(3)} hint={`≈ ${c.effective_n.toFixed(1)} effective positions`} />
            <StatCard label="Top 5 share" value={accountingPercent(c.top5_share)} hint="of included value" />
            <StatCard label="Top 10 share" value={accountingPercent(c.top10_share)} hint="of included value" />
            <StatCard
              label="Largest position"
              value={accountingPercent(c.max_position_weight)}
              hint={c.max_position_ticker}
            />
          </div>
          <ExplainerLegend />
        </>
      ) : null}

      <Section
        title="Sector weights"
        note={
          d.benchmark_available
            ? `vs ${d.benchmark} sector weights`
            : d.benchmark_reason === "unavailable"
              ? "benchmark weights pending — not yet published"
              : "benchmark comparison not included in this tier"
        }
      >
        <Table head={["Sector", "Weight", "Value", `${d.benchmark} weight`, "Over / under"]}>
          {d.sectors.map((s) => (
            <tr key={s.sector} className="border-b border-line last:border-0">
              <td className="px-4 py-2 font-medium">{s.sector}</td>
              <td className="px-4 py-2 text-right tabular-nums">{accountingPercent(s.weight)}</td>
              <td className="px-4 py-2 text-right tabular-nums text-muted">{moneyWhole(s.market_value, d.base_currency)}</td>
              <td className="px-4 py-2 text-right tabular-nums text-muted">{pct(s.benchmark_weight)}</td>
              <td className={`px-4 py-2 text-right tabular-nums ${s.delta != null && s.delta !== 0 ? (s.delta > 0 ? "text-positive" : "text-negative") : "text-muted"}`}>
                {deltaPct(s.delta)}
              </td>
            </tr>
          ))}
        </Table>
        {unclassifiedSector ? (
          <p className="mt-2 text-[11px] text-muted">
            {accountingPercent(unclassifiedSector.weight)} of included value has no resolved sector — shown as
            Unclassified, never assigned a guessed one. Set a sector override from the holding’s classification to
            place it.
          </p>
        ) : null}
      </Section>

      <Section title="Geography" note="by country of domicile">
        <Table head={["Region", "Weight", "Value"]}>
          {d.geography.map((g) => (
            <tr key={g.bucket} className="border-b border-line last:border-0">
              <td className="px-4 py-2 font-medium">{g.bucket}</td>
              <td className="px-4 py-2 text-right tabular-nums">{geoTotal > 0 ? accountingPercent(g.weight) : "—"}</td>
              <td className="px-4 py-2 text-right tabular-nums text-muted">{moneyWhole(g.market_value, d.base_currency)}</td>
            </tr>
          ))}
        </Table>
        <p className="mt-2 text-[11px] text-muted">
          A fund/ETF defaults to its listing domicile — not its underlying exposure; reclassify a broad-international
          fund via its Country override.
        </p>
      </Section>

      {/* User-target drift — rendered ONLY when the user has authored targets. The user
          wrote the rule; this section reports the measurement ("you set X; actual is Y"). */}
      {d.target_drift != null ? (
        <Section title="Your stated targets" note="your rules, measured — you authored these">
          <Table head={["Your rule", "Target", "Actual", "Status"]}>
            {d.target_drift.map((row) => {
              const status = driftStatus(row);
              return (
                <tr key={`${row.kind}:${row.label}`} className="border-b border-line last:border-0">
                  <td className="px-4 py-2 font-medium">{driftRuleLabel(row)}</td>
                  <td className="px-4 py-2 text-right tabular-nums text-muted">{pct(row.target)}</td>
                  <td className="px-4 py-2 text-right tabular-nums">{pct(row.actual)}</td>
                  <td className={`px-4 py-2 text-right ${status.className}`}>{status.text}</td>
                </tr>
              );
            })}
          </Table>
        </Section>
      ) : null}
    </>
  );
}
