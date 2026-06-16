// Holdings grouped by security type (metron-ops#47). Partitions holdings into
// cash / bonds / equities / … and renders one HoldingsTable per group — each table's
// existing totals row is that group's subtotal — under a per-group heading, with a
// portfolio grand-total bar on top. A single group renders as the bare table (no
// headings), preserving the prior look when everything is one asset class.
//
// Presentational + server-renderable: HoldingsTable (a client component) carries the
// sort/interaction; this wrapper only partitions and sums.

import { HoldingsTable } from "@/components/holdings-table";
import type { Holding } from "@/lib/api";
import { moneyWhole, percent, signClass, signedMoneyWhole } from "@/lib/format";

// Display order + labels for the security types classify_security_type emits.
const TYPE_LABELS: [string, string][] = [
  ["cash", "Cash"],
  ["bond", "Bonds & CDs"],
  ["equity", "Equities"],
  ["etf", "ETFs"],
  ["fund", "Funds"],
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
}: {
  holdings: Holding[];
  baseCurrency: string;
  priced: boolean;
  /** Threaded to HoldingsTable for the inline ticker-alias editor (metron-ops#47). */
  portfolioId?: string;
}) {
  const groups = groupByType(holdings);

  // One group → the plain table (its totals row is the total); no headings needed.
  if (groups.length <= 1) {
    return (
      <HoldingsTable holdings={holdings} baseCurrency={baseCurrency} priced={priced} portfolioId={portfolioId} />
    );
  }

  const grand = grandTotal(holdings);
  const grandPct = grand.unreal != null && grand.cost ? grand.unreal / grand.cost : null;

  return (
    <div className="space-y-5">
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
                {grand.unreal != null ? signedMoneyWhole(grand.unreal, baseCurrency) : "—"}
                {grandPct != null ? <span className="ml-1 text-xs">({percent(grandPct)})</span> : null}
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
          <HoldingsTable holdings={hs} baseCurrency={baseCurrency} priced={priced} portfolioId={portfolioId} />
        </div>
      ))}
    </div>
  );
}
