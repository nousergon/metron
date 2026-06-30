"use client";

// Faceted type-filter chips for the Holdings table (metron-ops#115). One chip per
// instrument type actually held; clicking hides/shows that type's rows. Session-only (it
// resets on reload) — a filter that hides holdings shouldn't silently persist across visits
// the way the layout (grouping / columns) does. Only types present in the holdings get a
// chip, so the row stays relevant.

// Canonical chip order + short plural labels, keyed by the security_type value the
// classifier emits. Mirrors the "By asset class" grouping order.
const CHIP_LABELS: [string, string][] = [
  ["equity", "Stocks"],
  ["etf", "ETFs"],
  ["fund", "Funds"],
  ["treasury", "Treasuries"],
  ["bond", "Bonds"],
  ["cd", "CDs"],
  ["cash", "Cash"],
  ["option", "Options"],
  ["other", "Other"],
];

/** Present types in canonical order, with any unrecognized type appended (never hidden by
 *  omission), each paired with its chip label + count. */
export function presentTypes(securityTypes: string[]): { type: string; label: string; count: number }[] {
  const counts = new Map<string, number>();
  for (const t of securityTypes) counts.set(t, (counts.get(t) ?? 0) + 1);
  const out: { type: string; label: string; count: number }[] = [];
  const seen = new Set<string>();
  for (const [type, label] of CHIP_LABELS) {
    if (counts.has(type)) {
      out.push({ type, label, count: counts.get(type)! });
      seen.add(type);
    }
  }
  for (const [type, count] of counts) {
    if (!seen.has(type)) out.push({ type, label: type, count }); // unrecognized → raw key
  }
  return out;
}

export function TypeFilterChips({
  securityTypes,
  hidden,
  onToggle,
}: {
  /** Every holding's security_type (pre-filter) — drives which chips exist + their counts. */
  securityTypes: string[];
  /** The set of types currently hidden. A chip is "on" when NOT in this set. */
  hidden: Set<string>;
  onToggle: (type: string) => void;
}) {
  const chips = presentTypes(securityTypes);
  // Nothing to filter when there's only one type.
  if (chips.length <= 1) return null;
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <span className="text-[10px] uppercase tracking-wide text-muted">Types</span>
      {chips.map(({ type, label, count }) => {
        const on = !hidden.has(type);
        return (
          <button
            key={type}
            type="button"
            onClick={() => onToggle(type)}
            aria-pressed={on}
            className={`rounded-full border px-2.5 py-0.5 text-xs transition ${
              on
                ? "border-accent/40 bg-accent/10 text-ink"
                : "border-line text-muted/70 line-through hover:text-ink"
            }`}
          >
            {label} <span className="tabular-nums opacity-60">{count}</span>
          </button>
        );
      })}
    </div>
  );
}
