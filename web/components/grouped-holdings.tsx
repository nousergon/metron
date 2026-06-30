// Holdings grouped by security type (metron-ops#47). Partitions holdings into
// cash / bonds / equities / … and renders one HoldingsTable per group — each table's
// existing totals row is that group's subtotal — under a per-group heading, with a
// portfolio grand-total bar on top. A single group renders as the bare table (no
// headings), preserving the prior look when everything is one asset class.
//
// Presentational + server-renderable: HoldingsTable (a client component) carries the
// sort/interaction; this wrapper only partitions and sums.

import { HoldingsTable, type MetricGroup } from "@/components/holdings-table";
import type { Holding } from "@/lib/api";
import { accountingMoneyWhole, accountingPercent, isoDate, moneyWhole, signClass } from "@/lib/format";

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

type GrandTotal = { cost: number | null; mv: number | null; unreal: number | null };

function grandTotal(holdings: Holding[]): GrandTotal {
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
  visibleMetricGroups,
  accountColumn,
}: {
  holdings: Holding[];
  baseCurrency: string;
  priced: boolean;
  /** Threaded to HoldingsTable for the inline ticker-alias editor (metron-ops#47). */
  portfolioId?: string;
  /** Column-preset bands threaded to every HoldingsTable (metron-ops#114). */
  visibleMetricGroups?: MetricGroup[];
  /** Uncombined per-account view — render the Account column (metron-ops#114). */
  accountColumn?: boolean;
}) {
  const groups = groupByType(holdings);

  // One group → the plain table (its totals row is the total); no headings needed.
  if (groups.length <= 1) {
    return (
      <div className="space-y-2">
        {priced ? <PricesAsOf holdings={holdings} /> : null}
        <HoldingsTable holdings={holdings} baseCurrency={baseCurrency} priced={priced} portfolioId={portfolioId} visibleMetricGroups={visibleMetricGroups} accountColumn={accountColumn} />
      </div>
    );
  }

  const grand = grandTotal(holdings);
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

      {groups.map(([label, hs]) => (
        <div key={label}>
          <h3 className="mb-2 flex items-baseline gap-2 text-sm font-medium">
            {label}
            <span className="text-xs text-muted">
              {hs.length} {hs.length === 1 ? "holding" : "holdings"}
            </span>
          </h3>
          <HoldingsTable holdings={hs} baseCurrency={baseCurrency} priced={priced} portfolioId={portfolioId} visibleMetricGroups={visibleMetricGroups} accountColumn={accountColumn} />
        </div>
      ))}
    </div>
  );
}
