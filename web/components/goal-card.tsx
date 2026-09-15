// Retirement-goal card (metron-ops-I316) — the Overview surface. Empty state links
// to the goal form on Settings rather than rendering any number: an unset goal has
// nothing arithmetic to show, and Metron never suggests one to fill the gap.

import Link from "next/link";
import { moneyWhole } from "@/lib/format";
import type { GoalFacets } from "@/lib/api-goal";

/** Absolute progress percent — never signed (percent() in lib/format.ts prefixes a
 * "+", meant for a delta; progress toward a goal is a magnitude, not a change). */
function progressPercent(ratio: number): string {
  return `${(ratio * 100).toFixed(1)}%`;
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-2 border-t border-line/60 py-2 first:border-t-0 first:pt-0">
      <span className="text-xs text-muted">{label}</span>
      <span className="text-sm tabular-nums">{children}</span>
    </div>
  );
}

export function GoalCard({ portfolioId, facets, ccy = "USD" }: { portfolioId: string; facets: GoalFacets; ccy?: string }) {
  const progress = facets.goal_progress;
  const trajectory = facets.goal_trajectory_range;
  const timing = facets.goal_timing_cost;
  const assetLocation = facets.goal_asset_location_drag;
  const withdrawal = facets.goal_withdrawal_readiness;

  if (!progress.available) {
    return (
      <div className="rounded-lg border border-line p-4 sm:p-5">
        <div className="text-xs uppercase tracking-wide text-muted">Retirement goal</div>
        <p className="mt-2 text-sm text-muted">
          No goal set yet.{" "}
          <Link href={`/portfolios/${portfolioId}/settings`} className="underline hover:text-ink">
            Set your retirement number
          </Link>{" "}
          to see progress, trajectory, and drag against it.
        </p>
      </div>
    );
  }

  const ratio = (progress.progress_ratio as number) ?? 0;
  const target = (progress.target_amount_usd as number) ?? 0;
  const currentValue = (progress.current_value_usd as number) ?? 0;

  return (
    <div className="rounded-lg border border-line p-4 sm:p-5">
      <div className="flex items-baseline justify-between gap-2">
        <div className="text-xs uppercase tracking-wide text-muted">Retirement goal</div>
        <span className="text-[11px] text-muted" title="Arithmetic against the goal you set. Describes your portfolio; not a plan or advice.">
          as of {progress.as_of}
        </span>
      </div>

      <div className="mt-1 text-2xl font-semibold tabular-nums">{progressPercent(ratio)}</div>
      <div className="mt-1 text-xs text-muted">
        {moneyWhole(currentValue, ccy)} of {moneyWhole(target, ccy)}
      </div>
      <div className="mt-2 h-2 w-full overflow-hidden rounded-full bg-line/60">
        <div className="h-full rounded-full bg-ink" style={{ width: `${Math.min(100, Math.max(0, ratio * 100))}%` }} />
      </div>

      <div className="mt-3">
        {trajectory.available ? (
          <Row label="Years to goal (your own returns)">
            {Math.round(trajectory.years_low as number)}–{Math.round(trajectory.years_high as number)}y
          </Row>
        ) : (
          <Row label="Years to goal">not enough history yet</Row>
        )}

        {timing.available ? (
          <Row label="Timing cost (MWR vs TWR)">
            {(timing.years_added as number) >= 0 ? "+" : ""}
            {(timing.years_added as number).toFixed(1)}y
          </Row>
        ) : null}

        <Row label="Fee drag">not available</Row>

        {assetLocation.available ? (
          <Row label="Taxable dividend/interest income">
            {moneyWhole(assetLocation.taxable_dividend_interest_income_usd as number, ccy)}/yr
          </Row>
        ) : null}

        {withdrawal.available ? (
          <Row label="Withdrawal readiness (tightest account)">
            {Math.min(
              ...Object.values(withdrawal.by_account_type as Record<string, { months_covered: number }>).map(
                (b) => b.months_covered,
              ),
            ).toFixed(0)}{" "}
            mo
          </Row>
        ) : null}
      </div>

      <p className="mt-3 text-[11px] text-muted">
        Arithmetic against the goal you set. Describes your portfolio; not a plan or advice.
      </p>
    </div>
  );
}
