// Holdings grouped by ACCOUNT (metron-ops#114): the uncombined per-account rows sectioned
// per account, each section a HoldingsTable whose own totals row is that account's subtotal,
// under an account heading, with a portfolio grand-total bar on top. The "review each
// account" view — mirrors GroupedHoldings (by asset class) but partitions by account_label.
//
// Only meaningful on the by-account data (rows carry account_label); HoldingsView shows the
// "By account" grouping option only when the Combine toggle is set to By-account. The
// account column is suppressed here — the section heading already names the account.

import { HoldingsTable, type MetricGroup } from "@/components/holdings-table";
import type { Holding } from "@/lib/api";
import { accountingMoneyWhole, accountingPercent, isoDate, moneyWhole, signClass } from "@/lib/format";

const UNASSIGNED = "Unassigned";

/** The latest close date across priced holdings + whether the close feed has stalled. */
function priceFreshness(holdings: Holding[]): { asOf: string | null; stale: boolean } {
  let asOf: string | null = null;
  let stale = false;
  for (const h of holdings) {
    if (h.last_price_date && (asOf === null || h.last_price_date > asOf)) asOf = h.last_price_date;
    if (h.last_price_stale) stale = true;
  }
  return { asOf, stale };
}

function PricesAsOf({ holdings }: { holdings: Holding[] }) {
  const { asOf, stale } = priceFreshness(holdings);
  if (!asOf) return null;
  if (stale) {
    return (
      <p className="rounded border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-200">
        ⚠ Prices as of {isoDate(asOf)} — the market-data feed hasn’t updated since, so
        market values may be stale.
      </p>
    );
  }
  return <p className="text-xs text-muted">Prices as of {isoDate(asOf)}.</p>;
}

type Total = { cost: number | null; mv: number | null; unreal: number | null };

function totalsOf(holdings: Holding[]): Total {
  let cost = 0;
  let mv = 0;
  let unreal = 0;
  let haveCost = false;
  let haveMv = false;
  let haveUnreal = false;
  for (const h of holdings) {
    if (h.cost_basis_base != null) {
      cost += h.cost_basis_base;
      haveCost = true;
    }
    if (h.market_value != null) {
      mv += h.market_value;
      haveMv = true;
    }
    if (h.unrealized_gain != null) {
      unreal += h.unrealized_gain;
      haveUnreal = true;
    }
  }
  return { cost: haveCost ? cost : null, mv: haveMv ? mv : null, unreal: haveUnreal ? unreal : null };
}

/** Partition into [account_label, holdings] sections, ordered by market value desc (the
 *  biggest account first), then by label. A row with no account_label is never dropped —
 *  it falls into an "Unassigned" section. */
function groupByAccount(holdings: Holding[]): [string, Holding[]][] {
  const byAccount = new Map<string, Holding[]>();
  for (const h of holdings) {
    const key = h.account_label || UNASSIGNED;
    const bucket = byAccount.get(key);
    if (bucket) bucket.push(h);
    else byAccount.set(key, [h]);
  }
  return [...byAccount.entries()].sort((a, b) => {
    const ma = totalsOf(a[1]).mv ?? 0;
    const mb = totalsOf(b[1]).mv ?? 0;
    if (mb !== ma) return mb - ma;
    return a[0].localeCompare(b[0]);
  });
}

export function GroupedByAccount({
  holdings,
  baseCurrency,
  priced,
  portfolioId,
  visibleMetricGroups,
}: {
  holdings: Holding[];
  baseCurrency: string;
  priced: boolean;
  portfolioId?: string;
  visibleMetricGroups?: MetricGroup[];
}) {
  const groups = groupByAccount(holdings);

  // One account → the plain table (its totals row is the total); no headings needed.
  if (groups.length <= 1) {
    return (
      <div className="space-y-2">
        {priced ? <PricesAsOf holdings={holdings} /> : null}
        <HoldingsTable holdings={holdings} baseCurrency={baseCurrency} priced={priced} portfolioId={portfolioId} visibleMetricGroups={visibleMetricGroups} />
      </div>
    );
  }

  const grand = totalsOf(holdings);
  const grandPct = grand.unreal != null && grand.cost ? grand.unreal / grand.cost : null;

  return (
    <div className="space-y-5">
      {priced ? <PricesAsOf holdings={holdings} /> : null}
      <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-1 rounded-lg border border-line bg-surface px-4 py-3 text-sm">
        <span className="text-xs font-medium uppercase tracking-wide text-muted">Portfolio total</span>
        <div className="flex flex-wrap items-baseline gap-x-6 tabular-nums">
          <span>
            <span className="text-[10px] uppercase tracking-wide text-muted">Cost </span>
            {grand.cost != null ? moneyWhole(grand.cost, baseCurrency) : "—"}
          </span>
          {priced ? (
            <>
              <span>
                <span className="text-[10px] uppercase tracking-wide text-muted">Market </span>
                {grand.mv != null ? moneyWhole(grand.mv, baseCurrency) : "—"}
              </span>
              <span className={grand.unreal != null ? signClass(grand.unreal) : "text-muted"}>
                <span className="text-[10px] uppercase tracking-wide text-muted">Unrealized </span>
                {grand.unreal != null ? accountingMoneyWhole(grand.unreal, baseCurrency) : "—"}
                {grandPct != null ? <span className="ml-1 text-xs">{accountingPercent(grandPct)}</span> : null}
              </span>
            </>
          ) : null}
        </div>
      </div>

      {groups.map(([label, hs]) => {
        const sub = totalsOf(hs);
        return (
          <div key={label}>
            <h3 className="mb-2 flex flex-wrap items-baseline gap-2 text-sm font-medium">
              {label}
              <span className="text-xs text-muted">
                {hs.length} {hs.length === 1 ? "holding" : "holdings"}
              </span>
              {priced && sub.mv != null ? (
                <span className="text-xs text-muted">· {moneyWhole(sub.mv, baseCurrency)}</span>
              ) : null}
            </h3>
            <HoldingsTable holdings={hs} baseCurrency={baseCurrency} priced={priced} portfolioId={portfolioId} visibleMetricGroups={visibleMetricGroups} />
          </div>
        );
      })}
    </div>
  );
}
