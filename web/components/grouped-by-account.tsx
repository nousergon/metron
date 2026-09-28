// Holdings grouped by ACCOUNT (metron-ops#114): the uncombined per-account rows sectioned
// per account, each section a HoldingsTable whose own totals row is that account's subtotal,
// under an account heading, with a portfolio grand-total bar on top. The "review each
// account" view — mirrors GroupedHoldings (by asset class) but partitions by account_label.
//
// Only meaningful on the by-account data (rows carry account_label); HoldingsView shows the
// "By account" grouping option only when the Combine toggle is set to By-account. The
// account column is suppressed here — the section heading already names the account.

import type { ReactNode } from "react";
import { CollapsibleSection } from "@/components/collapsible-section";
import { HoldingsTable, type ColumnBand } from "@/components/holdings-table";
import { PortfolioTotalBar } from "@/components/portfolio-total-bar";
import { PositionsAsOfCaption, PricesAsOfCaption, StalePositionsWarning, StalePriceWarning } from "@/components/price-freshness";
import type { Holding } from "@/lib/api";
import { moneyWhole } from "@/lib/format";

const UNASSIGNED = "Unassigned";

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
  visibleBands,
  belowTotal,
  showTotal = true,
  freshnessCaptions = true,
}: {
  holdings: Holding[];
  baseCurrency: string;
  priced: boolean;
  portfolioId?: string;
  visibleBands?: ColumnBand[];
  /** Rendered under the Portfolio total bar (the column-band control, metron-ops#118+). */
  belowTotal?: ReactNode;
  /** false → the page renders the Portfolio total elsewhere (the landing page hoists it to
   *  the top); `belowTotal` then renders on its own, directly above the tables. */
  showTotal?: boolean;
  /** false → omit the plain "prices as of" / "positions synced" captions (the landing page
   *  renders them in its bottom notes). Stale WARNINGS always render inline. */
  freshnessCaptions?: boolean;
}) {
  const groups = groupByAccount(holdings);
  // Show the total bar whenever there's a control to anchor or multiple accounts to summarize.
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
        <HoldingsTable holdings={holdings} baseCurrency={baseCurrency} priced={priced} portfolioId={portfolioId} visibleBands={visibleBands} />
      ) : (
        groups.map(([label, hs]) => {
          const sub = totalsOf(hs);
          return (
            <CollapsibleSection
              key={label}
              summary={
                <h3 className="mb-2 flex flex-wrap items-baseline gap-2 text-sm font-medium">
                  {label}
                  <span className="text-xs text-muted">
                    {hs.length} {hs.length === 1 ? "holding" : "holdings"}
                  </span>
                  {priced && sub.mv != null ? (
                    <span className="text-xs text-muted">· {moneyWhole(sub.mv, baseCurrency)}</span>
                  ) : null}
                </h3>
              }
            >
              <HoldingsTable holdings={hs} baseCurrency={baseCurrency} priced={priced} portfolioId={portfolioId} visibleBands={visibleBands} />
            </CollapsibleSection>
          );
        })
      )}
    </div>
  );
}
