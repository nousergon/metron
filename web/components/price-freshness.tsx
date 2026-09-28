// Price + positions freshness captions for the Holdings groupers and the landing page's
// bottom notes (extracted from the identical copies grouped-holdings.tsx and
// grouped-by-account.tsx each carried).
//
// Two tiers, deliberately separable:
//   - the AMBER WARNINGS (a holding's close feed stalled / a broker re-sync fell behind)
//     are alerts and stay inline above the holdings table;
//   - the PLAIN CAPTIONS ("Prices as of …", "Positions synced through …") are provenance
//     notes — on the landing page they live in the notes block at the bottom of the page.
//
// Stale-price warning scope (fix, 2026-09-28): the warning used to print the FRESHEST
// close date across all holdings ("Prices as of Sep 28 — the market-data feed hasn't
// updated since") while being triggered by ANY single holding's `last_price_stale`. With
// the live overlay stamping most rows with today's date, one lagging close-fed holding
// produced a banner claiming the whole feed had stalled as of today — self-contradictory
// on a normal trading morning. It now names the stale holding(s) and their OWN last close,
// so the claim is exactly as wide as the evidence.

import type { Holding } from "@/lib/api";
import { isoDate } from "@/lib/format";

/** The latest close date across priced holdings (the caption's "as of"). */
export function latestPriceDate(holdings: Holding[]): string | null {
  let asOf: string | null = null;
  for (const h of holdings) {
    if (h.last_price_date && (asOf === null || h.last_price_date > asOf)) asOf = h.last_price_date;
  }
  return asOf;
}

export type StalePrice = { label: string; date: string | null };

/** Holdings the server flagged stale (close-fed and ≥ STALE_AFTER_SESSIONS behind), one
 *  entry per display label (the by-account view repeats a ticker per account), keeping the
 *  OLDEST close date for that label, oldest first. */
export function stalePrices(holdings: Holding[]): StalePrice[] {
  const byLabel = new Map<string, string | null>();
  for (const h of holdings) {
    if (!h.last_price_stale) continue;
    const label = h.user_label || h.ticker;
    const d = h.last_price_date;
    if (!byLabel.has(label)) {
      byLabel.set(label, d);
    } else if (d != null) {
      const prev = byLabel.get(label);
      if (prev == null || d < prev) byLabel.set(label, d);
    }
  }
  return [...byLabel.entries()]
    .map(([label, date]) => ({ label, date }))
    .sort((a, b) => (a.date ?? "").localeCompare(b.date ?? "") || a.label.localeCompare(b.label));
}

/** The OLDEST broker sync date across snapshot-sourced holdings (the worst-case
 *  contributor, not the freshest — a stale account must not hide behind a fresh one) +
 *  whether any holding's position sync is stale (metron-ops#150). null when every
 *  holding is ledger-derived (CSV/OFX), which has no broker snapshot to go stale. */
export function positionsFreshness(holdings: Holding[]): { asOf: string | null; stale: boolean } {
  let asOf: string | null = null;
  let stale = false;
  for (const h of holdings) {
    if (h.broker_as_of && (asOf === null || h.broker_as_of < asOf)) asOf = h.broker_as_of;
    if (h.positions_stale) stale = true;
  }
  return { asOf, stale };
}

const AMBER = "rounded border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-200";

/** Amber alert naming each holding whose close-fed price has stalled — null when none has. */
export function StalePriceWarning({ holdings }: { holdings: Holding[] }) {
  const stale = stalePrices(holdings);
  if (stale.length === 0) return null;
  const one = stale.length === 1;
  return (
    <p className={AMBER} role="status">
      ⚠ Stale price{one ? "" : "s"} for {one ? "" : `${stale.length} holdings: `}
      {stale.map((s, i) => (
        <span key={s.label}>
          {i > 0 ? ", " : ""}
          <span className="font-medium">{s.label}</span>
          {s.date ? ` (last close ${isoDate(s.date)})` : ""}
        </span>
      ))}{" "}
      — the market-data feed hasn’t updated {one ? "it" : "them"} since, so {one ? "its market value" : "their market values"}{" "}
      may be stale.
    </p>
  );
}

/** Plain "Prices as of {latest close}" caption — the EOD valuation date is never implicit. */
export function PricesAsOfCaption({ holdings }: { holdings: Holding[] }) {
  const asOf = latestPriceDate(holdings);
  if (!asOf) return null;
  return <p className="text-xs text-muted">Prices as of {isoDate(asOf)}.</p>;
}

/** Amber alert when the daily broker re-sync has fallen behind, so a real trade at the
 *  broker can't silently sit unreflected behind a fresh-looking price. */
export function StalePositionsWarning({ holdings }: { holdings: Holding[] }) {
  const { asOf, stale } = positionsFreshness(holdings);
  if (!asOf || !stale) return null;
  return (
    <p className={AMBER} role="status">
      ⚠ Positions synced through {isoDate(asOf)} — a more recent trade at the broker may
      not be reflected yet.
    </p>
  );
}

/** Plain "Positions synced through {date}" caption — DISTINCT from the price caption: this
 *  is about how current the broker-reported SHARE COUNT is, not the per-share price. Omitted
 *  when the stale warning already carries the same date. */
export function PositionsAsOfCaption({ holdings }: { holdings: Holding[] }) {
  const { asOf, stale } = positionsFreshness(holdings);
  if (!asOf || stale) return null;
  return <p className="text-xs text-muted">Positions synced through {isoDate(asOf)}.</p>;
}
