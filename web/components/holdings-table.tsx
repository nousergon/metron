"use client";

// Sortable holdings table, shared by the portfolio page and the account
// drill-down. All money columns render in the portfolio base currency; the FX
// column carries each holding's native currency and the cached rate used for
// conversion. When a foreign holding has no cached FX rate the native value is
// shown muted with a `*` (never silently treated as base currency).

import { useMemo, useState, type ReactNode } from "react";
import type { Holding } from "@/lib/api";
import { fxRate, money, percent, quantity, signClass, signedMoney } from "@/lib/format";

type SortValue = string | number | null;

type Column = {
  key: string;
  label: string;
  pricedOnly?: boolean;
  /** Numeric columns open descending (biggest positions first). */
  defaultDesc?: boolean;
  /** Sort value — base-currency where available, native as a stable fallback. */
  value: (h: Holding) => SortValue;
};

const COLUMNS: Column[] = [
  { key: "ticker", label: "Ticker", value: (h) => h.ticker },
  { key: "fx", label: "FX", value: (h) => h.currency },
  { key: "quantity", label: "Quantity", defaultDesc: true, value: (h) => h.quantity },
  {
    key: "avg_cost",
    label: "Avg cost",
    defaultDesc: true,
    value: (h) => (h.fx_rate != null ? h.avg_cost * h.fx_rate : h.avg_cost),
  },
  { key: "cost_basis", label: "Cost basis", defaultDesc: true, value: (h) => h.cost_basis_base ?? h.cost_basis },
  {
    key: "last",
    label: "Last",
    pricedOnly: true,
    defaultDesc: true,
    value: (h) => (h.last_price != null && h.fx_rate != null ? h.last_price * h.fx_rate : h.last_price),
  },
  {
    key: "market_value",
    label: "Market value",
    pricedOnly: true,
    defaultDesc: true,
    value: (h) => h.market_value ?? h.market_value_local,
  },
  { key: "unrealized", label: "Unrealized $", pricedOnly: true, defaultDesc: true, value: (h) => h.unrealized_gain },
  { key: "unrealized_pct", label: "Unrealized %", pricedOnly: true, defaultDesc: true, value: (h) => h.unrealized_pct },
];

export function HoldingsTable({
  holdings,
  baseCurrency,
  priced,
}: {
  holdings: Holding[];
  baseCurrency: string;
  priced: boolean;
}) {
  const columns = priced ? COLUMNS : COLUMNS.filter((c) => !c.pricedOnly);
  const [sort, setSort] = useState<{ key: string; desc: boolean } | null>(null);

  const sorted = useMemo(() => {
    if (!sort) return holdings;
    const col = COLUMNS.find((c) => c.key === sort.key);
    if (!col) return holdings;
    return [...holdings].sort((a, b) => {
      const va = col.value(a);
      const vb = col.value(b);
      if (va == null && vb == null) return 0;
      if (va == null) return 1; // nulls last in either direction
      if (vb == null) return -1;
      const cmp =
        typeof va === "string" || typeof vb === "string"
          ? String(va).localeCompare(String(vb))
          : va - (vb as number);
      return sort.desc ? -cmp : cmp;
    });
  }, [holdings, sort]);

  function toggle(col: Column) {
    setSort((s) => (s?.key === col.key ? { key: col.key, desc: !s.desc } : { key: col.key, desc: !!col.defaultDesc }));
  }

  return (
    <div className="overflow-x-auto rounded-lg border border-line">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-line bg-surface text-left text-xs uppercase tracking-wide text-muted">
            {columns.map((col, i) => (
              <th key={col.key} className={`px-4 py-2 font-medium ${i === 0 ? "" : "text-right"}`}>
                <button
                  type="button"
                  onClick={() => toggle(col)}
                  className="inline-flex items-center gap-1 uppercase tracking-wide hover:text-ink"
                  title={`Sort by ${col.label}`}
                >
                  {col.label}
                  <span className={sort?.key === col.key ? "" : "invisible"}>{sort?.desc ? "▼" : "▲"}</span>
                </button>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sorted.map((h) => {
            const foreign = h.currency !== baseCurrency;
            // Base-currency cell with the no-fabrication fallback: muted native + `*`
            // when a foreign holding has no cached FX rate.
            const baseMoney = (base: number | null, native: number | null): ReactNode => {
              if (base != null) return money(base, baseCurrency);
              if (native == null) return "—";
              if (!foreign) return money(native, baseCurrency);
              return (
                <span className="text-muted" title={`No ${baseCurrency} FX rate cached`}>
                  {money(native, h.currency)}*
                </span>
              );
            };
            const avgCostBase = h.fx_rate != null ? h.avg_cost * h.fx_rate : null;
            const lastBase = h.last_price != null && h.fx_rate != null ? h.last_price * h.fx_rate : null;
            return (
              <tr key={h.ticker} className="border-b border-line last:border-0">
                <td className="px-4 py-2 font-medium">{h.ticker}</td>
                <td className="px-4 py-2 text-right text-muted">
                  {foreign ? `${h.currency} @ ${h.fx_rate != null ? fxRate(h.fx_rate) : "—"}` : h.currency}
                </td>
                <td className="px-4 py-2 text-right tabular-nums">{quantity(h.quantity)}</td>
                <td className="px-4 py-2 text-right tabular-nums">{baseMoney(avgCostBase, h.avg_cost)}</td>
                <td className="px-4 py-2 text-right tabular-nums">{baseMoney(h.cost_basis_base, h.cost_basis)}</td>
                {priced ? (
                  <>
                    <td className="px-4 py-2 text-right tabular-nums text-muted">
                      {baseMoney(lastBase, h.last_price)}
                    </td>
                    <td className="px-4 py-2 text-right tabular-nums">
                      {baseMoney(h.market_value, h.market_value_local)}
                    </td>
                    <td className={`px-4 py-2 text-right tabular-nums ${signClass(h.unrealized_gain ?? 0)}`}>
                      {h.unrealized_gain != null ? signedMoney(h.unrealized_gain, baseCurrency) : "—"}
                    </td>
                    <td className={`px-4 py-2 text-right tabular-nums ${signClass(h.unrealized_pct ?? 0)}`}>
                      {h.unrealized_pct != null ? percent(h.unrealized_pct) : "—"}
                    </td>
                  </>
                ) : null}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
