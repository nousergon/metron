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
import { PositionsAsOfCaption, PricesAsOfCaption, StalePositionsWarning, StalePriceWarning } from "@/components/price-freshness";
import type { Holding } from "@/lib/api";

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
  showTotal = true,
  freshnessCaptions = true,
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
  /** false → the page renders the Portfolio total elsewhere (the landing page hoists it to
   *  the top); `belowTotal` then renders on its own, directly above the tables. */
  showTotal?: boolean;
  /** false → omit the plain "prices as of" / "positions synced" captions (the landing page
   *  renders them in its bottom notes). Stale WARNINGS always render inline. */
  freshnessCaptions?: boolean;
}) {
  const groups = groupByType(holdings);
  // Show the total bar whenever there's a control to anchor or multiple groups to summarize.
  const showBar = belowTotal != null || groups.length > 1;

  return (
    <div className="space-y-5">
      {priced ? <StalePriceWarning holdings={holdings} /> : null}
      <StalePositionsWarning holdings={holdings} />
      {freshnessCaptions && priced ? <PricesAsOfCaption holdings={holdings} /> : null}
      {freshnessCaptions ? <PositionsAsOfCaption holdings={holdings} /> : null}
      {!showTotal ? belowTotal : showBar ? (
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
