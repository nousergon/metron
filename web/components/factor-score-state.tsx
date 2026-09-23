// Explicit substrate states for the factor-attractiveness score (metron-ops-I334).
//
// The "Factor score" reads the v1 factor-profile substrate, which can be in two states a
// blank cell cannot express: STALE (the score is still returned, but the profiles are past
// their 8-day cadence) and RETIRED (`settings.retired_v1_surfaces` is on and the universe is
// empty). A surface with no live producer renders as explicitly retired, never as a stale
// number presented as current and never silently blank — and both stay visually distinct
// from a per-ticker coverage gap ("—"), which is a legitimate state of its own.

export type FactorScoreState = "stale" | "retired" | null;

export const FACTOR_STALE_TITLE =
  "Stale — the factor profiles behind this score are more than 8 days old; shown as the last published value, not a current one.";

export const FACTOR_RETIRED_TITLE =
  "Factor attractiveness retired — v1 factor substrate discontinued.";

/** Substrate state from the payload flags. Retired wins: once cut over there is no score. */
export function factorScoreState(flags: { stale?: boolean | null; retired?: boolean | null }): FactorScoreState {
  if (flags.retired) return "retired";
  if (flags.stale) return "stale";
  return null;
}

/** Muted "stale" tag rendered beside a score that is still shown but no longer current. */
export function FactorStaleTag({ title = FACTOR_STALE_TITLE }: { title?: string }) {
  return (
    <span
      className="ml-1 whitespace-nowrap rounded border border-line px-1 py-px text-[10px] uppercase tracking-wider text-muted"
      title={title}
      data-factor-state="stale"
    >
      stale
    </span>
  );
}

/** Muted "retired" label rendered in place of a score once the substrate is discontinued. */
export function FactorRetiredLabel({ title = FACTOR_RETIRED_TITLE }: { title?: string }) {
  return (
    <span className="whitespace-nowrap text-muted italic" title={title} data-factor-state="retired">
      retired
    </span>
  );
}
