"use client";

// Sortable holdings table, shared by the portfolio page and the account
// drill-down. All money columns render in the portfolio base currency; the FX
// column carries each holding's native currency and the cached rate used for
// conversion. When a foreign holding has no cached FX rate the native value is
// shown muted with a `*` (never silently treated as base currency).

import { useMemo, useState, useTransition, type ReactNode } from "react";
import { useRouter } from "next/navigation";
import type { Holding } from "@/lib/api";
import { fxRate, money, moneyWhole, percent, quantity, signClass, signedMoneyWhole } from "@/lib/format";
import { setSecurityLabelAction } from "@/app/portfolios/[id]/actions";

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
  portfolioId,
}: {
  holdings: Holding[];
  baseCurrency: string;
  priced: boolean;
  /** When set, the Ticker cell exposes an inline alias editor (metron-ops#47). */
  portfolioId?: string;
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

  // Portfolio totals over the base-currency aggregates. A foreign holding with no
  // cached FX rate has no base value — it is EXCLUDED from the sum (never fabricated as
  // base), and the totals row flags `*` so the number is read as a partial total.
  const totals = useMemo(() => {
    let cost = 0;
    let mv = 0;
    let unreal = 0;
    let excluded = 0;
    for (const h of holdings) {
      const foreign = h.currency !== baseCurrency;
      const costBase = h.cost_basis_base ?? (foreign ? null : h.cost_basis);
      const mvBase = h.market_value ?? (foreign ? null : h.market_value_local);
      if (costBase != null) cost += costBase;
      else excluded += 1;
      if (priced) {
        if (mvBase != null) mv += mvBase;
        if (h.unrealized_gain != null) unreal += h.unrealized_gain;
      }
    }
    // Aggregate unrealized % is the gain over the summed cost basis (portfolio return).
    const unrealPct = priced && cost !== 0 ? unreal / cost : null;
    return { cost, mv, unreal, unrealPct, excluded };
  }, [holdings, baseCurrency, priced]);

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
            // when a foreign holding has no cached FX rate. `fmt` is money() for per-unit
            // prices (cents) or moneyWhole() for aggregates (metron-ops#45).
            const baseMoney = (
              base: number | null,
              native: number | null,
              fmt: (v: number, c: string) => string = money,
            ): ReactNode => {
              if (base != null) return fmt(base, baseCurrency);
              if (native == null) return "—";
              if (!foreign) return fmt(native, baseCurrency);
              return (
                <span className="text-muted" title={`No ${baseCurrency} FX rate cached`}>
                  {fmt(native, h.currency)}*
                </span>
              );
            };
            const avgCostBase = h.fx_rate != null ? h.avg_cost * h.fx_rate : null;
            const lastBase = h.last_price != null && h.fx_rate != null ? h.last_price * h.fx_rate : null;
            return (
              <tr key={h.ticker} className="border-b border-line last:border-0">
                <td className="px-4 py-2 font-medium">
                  <TickerCell h={h} portfolioId={portfolioId} />
                </td>
                <td className="px-4 py-2 text-right text-muted">
                  {foreign ? `${h.currency} @ ${h.fx_rate != null ? fxRate(h.fx_rate) : "—"}` : h.currency}
                </td>
                <td className="px-4 py-2 text-right tabular-nums">{quantity(h.quantity)}</td>
                <td className="px-4 py-2 text-right tabular-nums">{baseMoney(avgCostBase, h.avg_cost)}</td>
                <td className="px-4 py-2 text-right tabular-nums">
                  {baseMoney(h.cost_basis_base, h.cost_basis, moneyWhole)}
                </td>
                {priced ? (
                  <>
                    <td className="px-4 py-2 text-right tabular-nums text-muted">
                      {baseMoney(lastBase, h.last_price)}
                    </td>
                    <td className="px-4 py-2 text-right tabular-nums">
                      {baseMoney(h.market_value, h.market_value_local, moneyWhole)}
                    </td>
                    <td className={`px-4 py-2 text-right tabular-nums ${signClass(h.unrealized_gain ?? 0)}`}>
                      {h.unrealized_gain != null ? signedMoneyWhole(h.unrealized_gain, baseCurrency) : "—"}
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
        {holdings.length > 0 ? (
          <tfoot>
            <tr className="border-t border-line bg-surface font-medium">
              <td className="px-4 py-2" title={totals.excluded > 0 ? `${totals.excluded} holding(s) excluded — no ${baseCurrency} FX rate cached` : undefined}>
                Total{totals.excluded > 0 ? "*" : ""}
              </td>
              <td className="px-4 py-2" />
              <td className="px-4 py-2" />
              <td className="px-4 py-2" />
              <td className="px-4 py-2 text-right tabular-nums">{moneyWhole(totals.cost, baseCurrency)}</td>
              {priced ? (
                <>
                  <td className="px-4 py-2" />
                  <td className="px-4 py-2 text-right tabular-nums">{moneyWhole(totals.mv, baseCurrency)}</td>
                  <td className={`px-4 py-2 text-right tabular-nums ${signClass(totals.unreal)}`}>
                    {signedMoneyWhole(totals.unreal, baseCurrency)}
                  </td>
                  <td className={`px-4 py-2 text-right tabular-nums ${signClass(totals.unrealPct ?? 0)}`}>
                    {totals.unrealPct != null ? percent(totals.unrealPct) : "—"}
                  </td>
                </>
              ) : null}
            </tr>
          </tfoot>
        ) : null}
      </table>
    </div>
  );
}

/** The Ticker cell: shows a user alias (with the raw symbol beneath) when set, else the
 *  symbol. With a portfolioId it exposes an inline editor so an opaque numeric-CUSIP bond
 *  can be named (metron-ops#47). Read-only contexts (no portfolioId) just render. */
function TickerCell({ h, portfolioId }: { h: Holding; portfolioId?: string }) {
  const router = useRouter();
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(h.user_label ?? "");
  const [error, setError] = useState<string | null>(null);
  const [pending, start] = useTransition();

  if (!portfolioId) {
    // Read-only: alias (if any) over the symbol.
    return h.user_label ? (
      <span>
        {h.user_label}
        <span className="ml-1 text-xs font-normal text-muted">{h.ticker}</span>
      </span>
    ) : (
      <span>{h.ticker}</span>
    );
  }

  function save() {
    setError(null);
    start(async () => {
      const r = await setSecurityLabelAction(portfolioId!, h.ticker, value);
      if (!r.ok) {
        setError(r.message);
        return;
      }
      setEditing(false);
      router.refresh();
    });
  }

  if (editing) {
    return (
      <span className="inline-flex items-center gap-1">
        <input
          autoFocus
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") save();
            if (e.key === "Escape") {
              setValue(h.user_label ?? "");
              setEditing(false);
            }
          }}
          placeholder={h.ticker}
          aria-label={`Label for ${h.ticker}`}
          disabled={pending}
          className="w-32 rounded border border-line bg-surface px-1.5 py-0.5 text-sm font-normal"
        />
        <button type="button" onClick={save} disabled={pending} className="text-xs text-accent hover:underline">
          Save
        </button>
        {error ? <span className="text-xs text-negative">{error}</span> : null}
      </span>
    );
  }

  return (
    <span className="group inline-flex items-baseline gap-1">
      {h.user_label ? (
        <>
          <span>{h.user_label}</span>
          <span className="text-xs font-normal text-muted">{h.ticker}</span>
        </>
      ) : (
        <span>{h.ticker}</span>
      )}
      <button
        type="button"
        onClick={() => {
          setValue(h.user_label ?? "");
          setEditing(true);
        }}
        aria-label={`${h.user_label ? "Edit" : "Add"} label for ${h.ticker}`}
        title={h.user_label ? "Edit label" : "Add a label/alias"}
        className="text-xs font-normal text-muted opacity-0 transition hover:text-ink group-hover:opacity-100"
      >
        ✎
      </button>
    </span>
  );
}
