// Small presentational primitives — cards, stat tiles, and a simple table — kept
// dependency-free (plain Tailwind). Charting (Tremor) arrives with the Performance
// page, which is gated on a licensed price feed.

import Link from "next/link";
import type { ReactNode } from "react";

export function StatCard({
  label,
  value,
  hint,
  valueClass = "",
  href,
}: {
  label: string;
  value: string;
  hint?: string;
  valueClass?: string;
  /** When set, the whole card links to the supporting detail (metron-ops#59). */
  href?: string;
}) {
  const body = (
    <>
      <div className="flex items-center justify-between gap-1 text-xs uppercase tracking-wide text-muted">
        <span>{label}</span>
        {href ? <span aria-hidden="true">→</span> : null}
      </div>
      <div className={`mt-1 text-2xl font-semibold tabular-nums ${valueClass}`}>{value}</div>
      {hint ? <div className="mt-1 text-xs text-muted">{hint}</div> : null}
    </>
  );
  if (href) {
    return (
      <Link
        href={href}
        className="block rounded-lg border border-line p-4 transition hover:border-muted hover:bg-white/5"
      >
        {body}
      </Link>
    );
  }
  return <div className="rounded-lg border border-line p-4">{body}</div>;
}

export function Section({ title, children, note }: { title: string; children: ReactNode; note?: string }) {
  return (
    <section className="mt-8">
      <div className="flex items-baseline justify-between">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-muted">{title}</h2>
        {note ? <span className="text-xs text-muted">{note}</span> : null}
      </div>
      <div className="mt-3">{children}</div>
    </section>
  );
}

export function Table({ head, children }: { head: string[]; children: ReactNode }) {
  return (
    <div className="overflow-x-auto rounded-lg border border-line">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-line bg-surface text-left text-xs uppercase tracking-wide text-muted">
            {head.map((h, i) => (
              <th key={h} className={`px-4 py-2 font-medium ${i === 0 ? "" : "text-right"}`}>
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="rounded-lg border border-dashed border-line p-6 text-sm text-muted">{children}</div>;
}

/** Short inline explanation shown in place of a write-affordance (Import, rename, delete,
 * SnapTrade connect, price refresh…) on the Reference Rate showcase portfolio
 * (`isReferencePortfolio`, metron-ops#120). The portfolio is a live, real-tenant-visible
 * read-only mirror (metron#162) — the API already 403s every mutating route for it
 * (`api/main.py::_demo_read_only`), so this just replaces the dead-end click with a short
 * explanation instead of duplicating that check with clickable-but-broken buttons. */
export function ReadOnlyNotice({ children }: { children?: ReactNode }) {
  return (
    <p className="text-xs text-muted">{children ?? "Illustrative — read-only. This showcase portfolio can't be edited."}</p>
  );
}

// Upsell labels for a required tier (matches PortfolioNav's lock badge). Two exposed tiers
// now (metron-ops): a beta-excluded feature upsells to the full "AI Advisor" build.
const TIER_LABEL: Record<string, string> = { personal: "AI Advisor" };

/** Full-page placeholder for a feature excluded by the active product tier / data feed.
 * Renders when a gated route is navigated to directly (the nav already hides the link).
 * `reason` is the entitlement reason from /meta/entitlements: "tier" → the plan doesn't
 * include it; "feed"/"benchmark"/"etf_vendor" → it needs the licensed market-data feed. */
export function Locked({
  label,
  reason,
  requiredTier,
}: {
  label: string;
  reason: string | null;
  requiredTier: string | null;
}) {
  const tier = TIER_LABEL[requiredTier ?? ""] ?? requiredTier ?? "a higher";
  const needsFeed = reason !== "tier"; // feed / benchmark / etf_vendor all mean "licensed data"
  return (
    <div className="mt-6 rounded-lg border border-dashed border-line p-8 text-center">
      <div className="text-3xl" aria-hidden="true">
        🔒
      </div>
      <h1 className="mt-3 text-lg font-semibold">{label}</h1>
      <p className="mx-auto mt-2 max-w-md text-sm text-muted">
        {needsFeed ? (
          <>
            {label} needs the licensed market-data feed, included with the{" "}
            <span className="font-medium text-ink">{tier}</span> plan.
          </>
        ) : (
          <>
            {label} is part of the <span className="font-medium text-ink">{tier}</span> plan.
          </>
        )}
      </p>
      <p className="mt-4 text-[11px] uppercase tracking-[0.14em] text-muted/50">Available in {tier}</p>
    </div>
  );
}
