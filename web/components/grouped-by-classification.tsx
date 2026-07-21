// Holdings grouped by SECTOR → COUNTRY (Holdings metrics). Each sector is a section whose
// header band carries the SP1500-broad MEDIAN multiples for that sector (the peer
// benchmark), with the holdings nested under per-country sub-bands. Each leaf renders a
// HoldingsTable, so every holding sits visually beneath its sector's and country's median
// multiples for an at-a-glance rich/cheap read.
//
// Presentational; the median bands come from the feed-gated valuation-medians artifact
// (empty off a feed-entitled build → the bands show "—" but the grouping still works).

import type { ReactNode } from "react";
import { CollapsibleSection } from "@/components/collapsible-section";
import { HoldingsTable, type ColumnBand } from "@/components/holdings-table";
import { PortfolioTotalBar } from "@/components/portfolio-total-bar";
import type { GroupMedians, Holding, ValuationMedians } from "@/lib/api";
import { multiple, pct1 } from "@/lib/format";

const UNCLASSIFIED = "Unclassified";

/** Sum the base-currency market value of a group (for ordering sectors by size). */
function groupMv(holdings: Holding[]): number {
  return holdings.reduce((acc, h) => acc + (h.market_value ?? 0), 0);
}

/** Partition into [key, holdings] entries by the given field, biggest-MV group first; a
 *  null field value falls into "Unclassified" (sorted last). */
function partition(holdings: Holding[], field: "sector" | "country"): [string, Holding[]][] {
  const by = new Map<string, Holding[]>();
  for (const h of holdings) {
    const key = (field === "sector" ? h.sector : h.country) || UNCLASSIFIED;
    const bucket = by.get(key);
    if (bucket) bucket.push(h);
    else by.set(key, [h]);
  }
  return [...by.entries()].sort((a, b) => {
    if (a[0] === UNCLASSIFIED) return 1;
    if (b[0] === UNCLASSIFIED) return -1;
    return groupMv(b[1]) - groupMv(a[1]);
  });
}

/** The median-multiple band shown under a sector / country heading. Each field degrades to
 *  "—" when the producer had no usable sample (or off-feed). `n` is the peer-universe size. */
function MedianBand({ m, scope }: { m: GroupMedians | undefined; scope: string }) {
  if (!m) {
    return (
      <span className="text-xs text-muted">
        no {scope} median benchmark available
      </span>
    );
  }
  const cells: [string, string][] = [
    ["P/E", m.trailing_pe != null ? multiple(m.trailing_pe) : "—"],
    ["Fwd P/E", m.forward_pe != null ? multiple(m.forward_pe) : "—"],
    ["P/B", m.price_to_book != null ? multiple(m.price_to_book) : "—"],
    ["P/S", m.price_to_sales != null ? multiple(m.price_to_sales) : "—"],
    ["EV/EBITDA", m.ev_ebitda != null ? multiple(m.ev_ebitda) : "—"],
    ["Div", m.dividend_yield != null ? pct1(m.dividend_yield) : "—"],
  ];
  return (
    <span className="flex flex-wrap items-baseline gap-x-4 gap-y-1 text-xs tabular-nums text-muted">
      <span className="uppercase tracking-wide text-[10px]">{scope} median</span>
      {cells.map(([label, val]) => (
        <span key={label}>
          <span className="text-[10px] uppercase tracking-wide">{label} </span>
          {val}
        </span>
      ))}
      {m.n > 0 ? <span className="text-[10px]">n={m.n}</span> : null}
    </span>
  );
}

export function GroupedByClassification({
  holdings,
  baseCurrency,
  priced,
  medians,
  portfolioId,
  visibleBands,
  accountColumn,
  belowTotal,
}: {
  holdings: Holding[];
  baseCurrency: string;
  priced: boolean;
  medians: ValuationMedians | null;
  portfolioId?: string;
  /** Column-preset bands threaded to every HoldingsTable (metron-ops#114/#118+). */
  visibleBands?: ColumnBand[];
  /** Uncombined per-account view — render the Account column (metron-ops#114). */
  accountColumn?: boolean;
  /** Rendered under the Portfolio total bar (the column-band control, metron-ops#118+). */
  belowTotal?: ReactNode;
}) {
  const sectors = partition(holdings, "sector");

  return (
    <div className="space-y-6">
      <PortfolioTotalBar holdings={holdings} baseCurrency={baseCurrency} priced={priced} below={belowTotal} />
      {sectors.map(([sector, sectorHoldings]) => {
        const countries = partition(sectorHoldings, "country");
        return (
          <CollapsibleSection
            key={sector}
            className="space-y-3"
            summary={
              <div className="border-b border-line pb-1.5">
                <h3 className="flex items-baseline gap-2 text-sm font-semibold">
                  {sector}
                  <span className="text-xs font-normal text-muted">
                    {sectorHoldings.length} {sectorHoldings.length === 1 ? "holding" : "holdings"}
                  </span>
                </h3>
                <div className="mt-1">
                  <MedianBand m={medians?.by_sector?.[sector]} scope="sector" />
                </div>
              </div>
            }
          >
            <div className="space-y-3">
              {countries.map(([country, countryHoldings]) => (
                <div key={country} className="space-y-1.5 pl-3">
                  <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                    <h4 className="text-xs font-medium text-ink">{country}</h4>
                    <MedianBand m={medians?.by_country?.[country]} scope="country" />
                  </div>
                  <HoldingsTable
                    holdings={countryHoldings}
                    baseCurrency={baseCurrency}
                    priced={priced}
                    portfolioId={portfolioId}
                    visibleBands={visibleBands}
                    accountColumn={accountColumn}
                  />
                </div>
              ))}
            </div>
          </CollapsibleSection>
        );
      })}
    </div>
  );
}
