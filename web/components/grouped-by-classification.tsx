// Holdings grouped by SECTOR → COUNTRY (Holdings metrics). Each sector is a section whose
// header band carries the SP1500-broad MEDIAN multiples for that sector (the peer
// benchmark), with the holdings nested under per-country sub-bands. Each leaf renders a
// HoldingsTable, so every holding sits visually beneath its sector's and country's median
// multiples for an at-a-glance rich/cheap read.
//
// Presentational; the median bands come from the feed-gated valuation-medians artifact
// (empty off a feed-entitled build → the bands show "—" but the grouping still works).

import { HoldingsTable, type MetricGroup } from "@/components/holdings-table";
import type { GroupMedians, Holding, ValuationMedians } from "@/lib/api";
import { accountingMoneyWhole, moneyWhole, multiple, pct1, signClass } from "@/lib/format";

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

function GrandTotalBar({ holdings, baseCurrency, priced }: { holdings: Holding[]; baseCurrency: string; priced: boolean }) {
  let cost = 0;
  let mv = 0;
  let unreal = 0;
  let haveCost = false;
  let haveMv = false;
  let haveUnreal = false;
  for (const h of holdings) {
    if (h.cost_basis_base != null) { cost += h.cost_basis_base; haveCost = true; }
    if (h.market_value != null) { mv += h.market_value; haveMv = true; }
    if (h.unrealized_gain != null) { unreal += h.unrealized_gain; haveUnreal = true; }
  }
  const pct = haveUnreal && haveCost && cost ? unreal / cost : null;
  return (
    <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-1 rounded-lg border border-line bg-surface px-4 py-3 text-sm">
      <span className="text-xs font-medium uppercase tracking-wide text-muted">Portfolio total</span>
      <div className="flex flex-wrap items-baseline gap-x-6 tabular-nums">
        <span>
          <span className="text-[10px] uppercase tracking-wide text-muted">Cost </span>
          {haveCost ? moneyWhole(cost, baseCurrency) : "—"}
        </span>
        {priced ? (
          <>
            <span>
              <span className="text-[10px] uppercase tracking-wide text-muted">Market </span>
              {haveMv ? moneyWhole(mv, baseCurrency) : "—"}
            </span>
            <span className={haveUnreal ? signClass(unreal) : "text-muted"}>
              <span className="text-[10px] uppercase tracking-wide text-muted">Unrealized </span>
              {haveUnreal ? accountingMoneyWhole(unreal, baseCurrency) : "—"}
              {pct != null ? <span className="ml-1 text-xs">{pct1(pct)}</span> : null}
            </span>
          </>
        ) : null}
      </div>
    </div>
  );
}

export function GroupedByClassification({
  holdings,
  baseCurrency,
  priced,
  medians,
  portfolioId,
  visibleMetricGroups,
  accountColumn,
}: {
  holdings: Holding[];
  baseCurrency: string;
  priced: boolean;
  medians: ValuationMedians | null;
  portfolioId?: string;
  /** Column-preset bands threaded to every HoldingsTable (metron-ops#114). */
  visibleMetricGroups?: MetricGroup[];
  /** Uncombined per-account view — render the Account column (metron-ops#114). */
  accountColumn?: boolean;
}) {
  const sectors = partition(holdings, "sector");

  return (
    <div className="space-y-6">
      <GrandTotalBar holdings={holdings} baseCurrency={baseCurrency} priced={priced} />
      {sectors.map(([sector, sectorHoldings]) => {
        const countries = partition(sectorHoldings, "country");
        return (
          <div key={sector} className="space-y-3">
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
                  visibleMetricGroups={visibleMetricGroups}
                  accountColumn={accountColumn}
                />
              </div>
            ))}
          </div>
        );
      })}
    </div>
  );
}
