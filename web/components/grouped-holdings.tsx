// Holdings grouped by security type (metron-ops#47). Partitions holdings into
// cash / bonds / equities / … and renders one HoldingsTable per group — each table's
// existing totals row is that group's subtotal — under a per-group heading, with a shared
// portfolio grand-total bar on top (which also anchors the column-band control). A single
// group renders as the bare table (no per-group headings), preserving the prior look when
// everything is one asset class.
//
// Presentational + server-renderable: HoldingsTable (a client component) carries the
// sort/interaction; this wrapper only partitions and lays out.

import type { ReactNode } from "react";
import { CollapsibleSection } from "@/components/collapsible-section";
import { HoldingsTable, type ColumnBand } from "@/components/holdings-table";
import { PortfolioTotalBar } from "@/components/portfolio-total-bar";
import type { Holding } from "@/lib/api";
import { isoDate } from "@/lib/format";

/** The latest close date across priced holdings + whether the close feed has stalled
 *  (any holding flagged ≥1 full session stale by the server). */
function priceFreshness(holdings: Holding[]): { asOf: string | null; stale: boolean } {
  let asOf: string | null = null;
  let stale = false;
  for (const h of holdings) {
    if (h.last_price_date && (asOf === null || h.last_price_date > asOf)) asOf = h.last_price_date;
    if (h.last_price_stale) stale = true;
  }
  return { asOf, stale };
}

/** Always-on "prices as of {date}" caption so the EOD valuation date is never implicit;
 *  escalates to an amber warning when the upstream close feed has skipped a session, so a
 *  frozen feed fails loud instead of showing a stale price as if it were current. */
function PricesAsOf({ holdings }: { holdings: Holding[] }) {
  const { asOf, stale } = priceFreshness(holdings);
  if (!asOf) return null;
  if (stale) {
    // Boxed amber alert, matching the existing warning convention (import-panel /
    // performance) — a stalled feed should read as an alert, not a quiet footnote.
    return (
      <p className="rounded border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-200">
        ⚠ Prices as of {isoDate(asOf)} — the market-data feed hasn’t updated since, so
        market values may be stale.
      </p>
    );
  }
  return <p className="text-xs text-muted">Prices as of {isoDate(asOf)}.</p>;
}

/** The OLDEST broker sync date across snapshot-sourced holdings (the worst-case
 *  contributor, not the freshest — a stale account must not hide behind a fresh one) +
 *  whether any holding's position sync is stale (metron-ops#150). null when every
 *  holding is ledger-derived (CSV/OFX), which has no broker snapshot to go stale. */
function positionsFreshness(holdings: Holding[]): { asOf: string | null; stale: boolean } {
  let asOf: string | null = null;
  let stale = false;
  for (const h of holdings) {
    if (h.broker_as_of && (asOf === null || h.broker_as_of < asOf)) asOf = h.broker_as_of;
    if (h.positions_stale) stale = true;
  }
  return { asOf, stale };
}

/** "Positions synced through {date}" caption — DISTINCT from PricesAsOf: this is about
 *  how current the broker-reported SHARE COUNT is, not the per-share price. Escalates to
 *  an amber warning when the daily broker re-sync has fallen behind, so a real trade at
 *  the broker can't silently sit unreflected behind a fresh-looking price. */
function PositionsAsOf({ holdings }: { holdings: Holding[] }) {
  const { asOf, stale } = positionsFreshness(holdings);
  if (!asOf) return null;
  if (stale) {
    return (
      <p className="rounded border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-200">
        ⚠ Positions synced through {isoDate(asOf)} — a more recent trade at the broker may
        not be reflected yet.
      </p>
    );
  }
  return <p className="text-xs text-muted">Positions synced through {isoDate(asOf)}.</p>;
}

// Display order + labels for the security types classify_security_type emits. The
// fixed-income family is split into Treasuries / Bonds / CDs (metron-ops#114).
const TYPE_LABELS: [string, string][] = [
  ["equity", "Equities"],
  ["etf", "ETFs"],
  ["fund", "Funds"],
  ["treasury", "Treasuries"],
  ["bond", "Bonds"],
  ["cd", "CDs"],
  ["cash", "Cash"],
  ["option", "Options"],
  ["other", "Other"],
];

/** Partition into [label, holdings] groups in the canonical display order, with any
 *  unrecognized security_type appended (so a holding is never silently dropped). */
function groupByType(holdings: Holding[]): [string, Holding[]][] {
  const byType = new Map<string, Holding[]>();
  for (const h of holdings) {
    const key = h.security_type || "other";
    const bucket = byType.get(key);
    if (bucket) bucket.push(h);
    else byType.set(key, [h]);
  }
  const out: [string, Holding[]][] = [];
  const seen = new Set<string>();
  for (const [key, label] of TYPE_LABELS) {
    const hs = byType.get(key);
    if (hs) {
      out.push([label, hs]);
      seen.add(key);
    }
  }
  for (const [key, hs] of byType) {
    if (!seen.has(key)) out.push([key, hs]); // unrecognized type → its raw key as the label
  }
  return out;
}

export function GroupedHoldings({
  holdings,
  baseCurrency,
  priced,
  portfolioId,
  visibleBands,
  accountColumn,
  belowTotal,
}: {
  holdings: Holding[];
  baseCurrency: string;
  priced: boolean;
  /** Threaded to HoldingsTable for the inline ticker-alias editor (metron-ops#47). */
  portfolioId?: string;
  /** Column-preset bands threaded to every HoldingsTable (metron-ops#114/#118+). */
  visibleBands?: ColumnBand[];
  /** Uncombined per-account view — render the Account column (metron-ops#114). */
  accountColumn?: boolean;
  /** Rendered under the Portfolio total bar (the column-band control, metron-ops#118+). */
  belowTotal?: ReactNode;
}) {
  const groups = groupByType(holdings);
  // Show the total bar whenever there's a control to anchor or multiple groups to summarize.
  const showBar = belowTotal != null || groups.length > 1;

  return (
    <div className="space-y-5">
      {priced ? <PricesAsOf holdings={holdings} /> : null}
      <PositionsAsOf holdings={holdings} />
      {showBar ? (
        <PortfolioTotalBar holdings={holdings} baseCurrency={baseCurrency} priced={priced} below={belowTotal} />
      ) : null}
      {groups.length <= 1 ? (
        <HoldingsTable holdings={holdings} baseCurrency={baseCurrency} priced={priced} portfolioId={portfolioId} visibleBands={visibleBands} accountColumn={accountColumn} />
      ) : (
        groups.map(([label, hs]) => (
          <CollapsibleSection
            key={label}
            summary={
              <h3 className="mb-2 flex items-baseline gap-2 text-sm font-medium">
                {label}
                <span className="text-xs text-muted">
                  {hs.length} {hs.length === 1 ? "holding" : "holdings"}
                </span>
              </h3>
            }
          >
            <HoldingsTable holdings={hs} baseCurrency={baseCurrency} priced={priced} portfolioId={portfolioId} visibleBands={visibleBands} accountColumn={accountColumn} />
          </CollapsibleSection>
        ))
      )}
    </div>
  );
}
