"use client";

// "If sold" tax estimate (metron-ops#208) — the tax math of ONE hypothetical sale the user
// describes: which holding, how many shares, at what price, and which lots. Every number
// is server-owned (GET /tax/if-sold, read-only): proceeds, cost basis, the short-/long-term
// gain split per lot, and an estimated tax at flat rates.
//
// Binding compliance constraint: a calculator over a user-authored hypothetical. The panel
// never picks the holding, the quantity, or a lot — the ticker field starts empty, the
// specific-lot quantities start empty, and nothing is ordered by gain or loss. Copy stays
// factual ("if sold at", "estimate"). Locked by __tests__/if-sold-tax-panel.test.tsx.
//
// Rates: no per-user tax-rate preference exists yet, so the rates are inputs here. Left
// blank, the backend applies placeholder rates and names them; the panel then says, in
// plain words, that they are placeholders and not the user's rates.

import { useState, useTransition } from "react";
import type { IfSoldPreview, IfSoldSale, LotMethod } from "@/lib/api-tax-whatif";
import { fetchIfSoldAction } from "@/app/portfolios/[id]/tax-whatif-action";
import { accountingMoney, isoDate, money, quantity as fmtQty, signClass } from "@/lib/format";
import { CollapsibleSection } from "@/components/collapsible-section";

const RATE_LABEL: Record<string, string> = {
  short_term: "federal short-term",
  long_term: "federal long-term",
  state: "state",
};

const INPUT = "mt-1 rounded border border-line bg-paper px-2 py-1 text-right text-sm tabular-nums";

function pctLabel(fraction: number): string {
  return `${(fraction * 100).toFixed(2).replace(/\.?0+$/, "")}%`;
}

/** Parse an optional numeric input: "" → undefined, a bad/negative number → NaN. */
function parseOptional(raw: string): number | undefined {
  if (!raw.trim()) return undefined;
  const n = Number(raw);
  return Number.isFinite(n) && n >= 0 ? n : Number.NaN;
}

function signed(value: number, ccy: string): string {
  return accountingMoney(value, ccy);
}

function SaleFigures({ sale, ccy }: { sale: IfSoldSale; ccy: string }) {
  const cells: { label: string; value: string; cls?: string }[] = [
    { label: "Proceeds", value: money(sale.proceeds, ccy) },
    { label: "Cost basis", value: money(sale.cost_basis, ccy) },
    { label: "Short-term gain/loss", value: signed(sale.gain_st, ccy), cls: signClass(sale.gain_st) },
    { label: "Long-term gain/loss", value: signed(sale.gain_lt, ccy), cls: signClass(sale.gain_lt) },
    { label: "Estimated tax", value: money(sale.est_tax_total, ccy) },
  ];
  return (
    <div>
      <dl className="grid grid-cols-2 gap-3 sm:grid-cols-5">
        {cells.map((c) => (
          <div key={c.label} className="rounded-lg border border-line p-3">
            <dt className="text-xs uppercase tracking-wide text-muted">{c.label}</dt>
            <dd className={`mt-1 text-base font-medium tabular-nums ${c.cls ?? ""}`}>{c.value}</dd>
          </div>
        ))}
      </dl>
      <p className="mt-2 text-xs text-muted tabular-nums">
        Estimated tax = federal short-term {money(sale.est_tax_st, ccy)} + federal long-term{" "}
        {money(sale.est_tax_lt, ccy)} + state {money(sale.est_tax_state, ccy)}, on {money(sale.taxable_st, ccy)}{" "}
        short-term and {money(sale.taxable_lt, ccy)} long-term after netting one term&apos;s loss against the
        other&apos;s gain.
      </p>
    </div>
  );
}

function SoldLotsTable({ sale, ccy }: { sale: IfSoldSale; ccy: string }) {
  return (
    <div className="mt-3 overflow-x-auto rounded-lg border border-line">
      <table className="w-full text-sm" aria-label="Lots in the hypothetical sale">
        <thead>
          <tr className="border-b border-line bg-surface text-left text-xs uppercase tracking-wide text-muted">
            <th className="px-3 py-2 font-medium">Opened</th>
            <th className="px-3 py-2 text-right font-medium">Days held</th>
            <th className="px-3 py-2 text-right font-medium">Term</th>
            <th className="px-3 py-2 text-right font-medium">Quantity</th>
            <th className="px-3 py-2 text-right font-medium">Cost basis</th>
            <th className="px-3 py-2 text-right font-medium">Proceeds</th>
            <th className="px-3 py-2 text-right font-medium">Gain/loss</th>
          </tr>
        </thead>
        <tbody>
          {sale.lots.map((l) => (
            <tr key={`${l.lot_index}-${l.open_date}`} className="border-b border-line last:border-0">
              <td className="px-3 py-2">{isoDate(l.open_date)}</td>
              <td className="px-3 py-2 text-right tabular-nums">{l.holding_days}</td>
              <td className="px-3 py-2 text-right">{l.term}</td>
              <td className="px-3 py-2 text-right tabular-nums">{fmtQty(l.quantity)}</td>
              <td className="px-3 py-2 text-right tabular-nums">{money(l.cost_basis, ccy)}</td>
              <td className="px-3 py-2 text-right tabular-nums">{money(l.proceeds, ccy)}</td>
              <td className={`px-3 py-2 text-right tabular-nums ${signClass(l.gain)}`}>{signed(l.gain, ccy)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function DeltaLine({ preview }: { preview: IfSoldPreview }) {
  const d = preview.delta_vs_fifo;
  if (!d) return null;
  const ccy = preview.currency;
  return (
    <p className="mt-3 rounded-lg border border-line p-3 text-sm tabular-nums" data-testid="delta-vs-fifo">
      <span className="text-xs uppercase tracking-wide text-muted">Selected lots vs FIFO, same quantity: </span>
      cost basis {signed(d.cost_basis, ccy)} · short-term {signed(d.gain_st, ccy)} · long-term{" "}
      {signed(d.gain_lt, ccy)} · total gain/loss {signed(d.gain_total, ccy)} · estimated tax{" "}
      {signed(d.est_tax_total, ccy)} (FIFO estimate {money(preview.fifo.est_tax_total, ccy)})
    </p>
  );
}

function PanelBody({
  portfolioId,
  tickers,
  accountIds,
}: {
  portfolioId: string;
  tickers: string[];
  accountIds?: string[];
}) {
  const [ticker, setTicker] = useState("");
  const [qty, setQty] = useState("");
  const [price, setPrice] = useState("");
  const [stRate, setStRate] = useState("");
  const [ltRate, setLtRate] = useState("");
  const [stateRate, setStateRate] = useState("");
  const [method, setMethod] = useState<LotMethod>("fifo");
  const [picks, setPicks] = useState<Record<number, string>>({});
  const [preview, setPreview] = useState<IfSoldPreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();

  // The specific-lot picker needs the holding's lots, which arrive with a first estimate
  // for the SAME ticker.
  const lotsLoaded = preview != null && preview.ticker.toUpperCase() === ticker.trim().toUpperCase();
  const pickedTotal = Object.values(picks).reduce((a, v) => a + (Number(v) > 0 ? Number(v) : 0), 0);

  function changeTicker(next: string) {
    setTicker(next);
    setMethod("fifo");
    setPicks({});
  }

  function submit(e: React.FormEvent) {
    e.preventDefault();
    const quantity = parseOptional(qty);
    const px = parseOptional(price);
    const rates = [parseOptional(stRate), parseOptional(ltRate), parseOptional(stateRate)];
    if ([quantity, px].some((v) => v !== undefined && (Number.isNaN(v) || v < 0))) {
      setError("Quantity and price must be positive numbers.");
      return;
    }
    if (quantity === 0) {
      setError("Quantity must be greater than zero.");
      return;
    }
    if (rates.some((r) => r !== undefined && (Number.isNaN(r) || r > 100))) {
      setError("Rates are percentages between 0 and 100.");
      return;
    }
    const lots =
      method === "specific"
        ? Object.entries(picks)
            .map(([i, v]) => ({ lot_index: Number(i), quantity: Number(v) }))
            .filter((p) => Number.isFinite(p.quantity) && p.quantity > 0)
        : undefined;
    if (method === "specific" && (!lots || lots.length === 0)) {
      setError("Enter a quantity for at least one lot.");
      return;
    }
    const [st, lt, state] = rates.map((r) => (r === undefined ? undefined : r / 100));
    startTransition(async () => {
      const res = await fetchIfSoldAction(portfolioId, {
        ticker,
        quantity: method === "specific" ? pickedTotal : quantity,
        price: px,
        method,
        lots,
        st_rate: st,
        lt_rate: lt,
        state_rate: state,
        accountIds,
      });
      if (res.ok) {
        setPreview(res.preview);
        setError(null);
      } else {
        setError(res.message);
      }
    });
  }

  const ccy = preview?.currency ?? "USD";
  const listId = `if-sold-tickers-${portfolioId}`;

  return (
    <div className="rounded-lg border border-line p-4">
      <form onSubmit={submit} noValidate className="space-y-3">
        <div className="flex flex-wrap items-end gap-3">
          <label className="text-sm">
            <span className="block text-xs uppercase tracking-wide text-muted">Holding</span>
            <input
              type="text"
              list={listId}
              value={ticker}
              onChange={(e) => changeTicker(e.target.value)}
              placeholder="Ticker"
              aria-label="Holding to estimate"
              className="mt-1 w-28 rounded border border-line bg-paper px-2 py-1 text-sm uppercase"
            />
            <datalist id={listId}>
              {[...tickers].sort().map((t) => (
                <option key={t} value={t} />
              ))}
            </datalist>
          </label>
          <label className="text-sm">
            <span className="block text-xs uppercase tracking-wide text-muted">Quantity</span>
            <input
              type="number"
              inputMode="decimal"
              min={0}
              step="any"
              value={method === "specific" ? (pickedTotal ? String(pickedTotal) : "") : qty}
              onChange={(e) => setQty(e.target.value)}
              disabled={method === "specific"}
              placeholder={method === "specific" ? "Sum of lots" : "Full position"}
              aria-label="Hypothetical sale quantity"
              className={`${INPUT} w-32`}
            />
          </label>
          <label className="text-sm">
            <span className="block text-xs uppercase tracking-wide text-muted">If sold at</span>
            <input
              type="number"
              inputMode="decimal"
              min={0}
              step="any"
              value={price}
              onChange={(e) => setPrice(e.target.value)}
              placeholder="Latest close"
              aria-label="Hypothetical sale price"
              className={`${INPUT} w-32`}
            />
          </label>
        </div>

        <fieldset className="flex flex-wrap items-end gap-3">
          <legend className="text-xs uppercase tracking-wide text-muted">Your tax rates (%)</legend>
          {(
            [
              ["Federal short-term", stRate, setStRate],
              ["Federal long-term", ltRate, setLtRate],
              ["State (flat)", stateRate, setStateRate],
            ] as const
          ).map(([label, value, set]) => (
            <label key={label} className="text-sm">
              <span className="block text-xs text-muted">{label}</span>
              <input
                type="number"
                inputMode="decimal"
                min={0}
                max={100}
                step="any"
                value={value}
                onChange={(e) => set(e.target.value)}
                placeholder="Placeholder"
                aria-label={`${label} rate`}
                className={`${INPUT} w-28`}
              />
            </label>
          ))}
        </fieldset>

        <div className="flex flex-wrap items-center gap-3">
          <div className="inline-flex rounded-lg border border-line p-0.5 text-xs" role="group" aria-label="Lot method">
            <button
              type="button"
              onClick={() => setMethod("fifo")}
              aria-pressed={method === "fifo"}
              className={`rounded-md px-2.5 py-1 ${method === "fifo" ? "bg-white/10 font-medium" : "text-muted"}`}
            >
              FIFO (oldest lot first)
            </button>
            <button
              type="button"
              onClick={() => setMethod("specific")}
              aria-pressed={method === "specific"}
              disabled={!lotsLoaded}
              title={lotsLoaded ? undefined : "Estimate once to load this holding's lots"}
              className={`rounded-md px-2.5 py-1 disabled:opacity-50 ${method === "specific" ? "bg-white/10 font-medium" : "text-muted"}`}
            >
              Specific lots
            </button>
          </div>
          <button
            type="submit"
            disabled={pending || !ticker.trim()}
            className="rounded border border-line px-3 py-1.5 text-sm transition hover:border-muted hover:bg-white/5 disabled:opacity-50"
          >
            {pending ? "Estimating…" : "Estimate"}
          </button>
        </div>

        {method === "specific" && lotsLoaded && preview ? (
          <div className="overflow-x-auto rounded-lg border border-line">
            <table className="w-full text-sm" aria-label="Open lots to include">
              <thead>
                <tr className="border-b border-line bg-surface text-left text-xs uppercase tracking-wide text-muted">
                  <th className="px-3 py-2 font-medium">Opened</th>
                  <th className="px-3 py-2 text-right font-medium">Term</th>
                  <th className="px-3 py-2 text-right font-medium">Shares</th>
                  <th className="px-3 py-2 text-right font-medium">Cost / share</th>
                  <th className="px-3 py-2 text-right font-medium">Quantity to include</th>
                </tr>
              </thead>
              <tbody>
                {preview.open_lots.map((l) => (
                  <tr key={l.lot_index} className="border-b border-line last:border-0">
                    <td className="px-3 py-2">{isoDate(l.open_date)}</td>
                    <td className="px-3 py-2 text-right">{l.term}</td>
                    <td className="px-3 py-2 text-right tabular-nums">{fmtQty(l.quantity)}</td>
                    <td className="px-3 py-2 text-right tabular-nums">{money(l.cost_per_share, ccy)}</td>
                    <td className="px-3 py-2 text-right">
                      <input
                        type="number"
                        inputMode="decimal"
                        min={0}
                        max={l.quantity}
                        step="any"
                        value={picks[l.lot_index] ?? ""}
                        onChange={(e) => setPicks((p) => ({ ...p, [l.lot_index]: e.target.value }))}
                        aria-label={`Quantity from lot opened ${l.open_date}`}
                        className={`${INPUT} w-24`}
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </form>

      {error ? (
        <p className="mt-3 text-sm text-muted" role="alert">
          {error}
        </p>
      ) : null}

      {preview ? (
        <div className="mt-4 space-y-3">
          <p className="text-sm tabular-nums">
            If {fmtQty(preview.sale.quantity)} of {fmtQty(preview.quantity_held)} sh {preview.ticker} were sold at{" "}
            {money(preview.price, ccy)}{" "}
            <span className="text-muted">
              ({preview.price_source === "latest_close"
                ? `latest close${preview.price_as_of ? `, ${isoDate(preview.price_as_of)}` : ""}`
                : "price you entered"}
              ; {preview.sale.method === "fifo" ? "FIFO" : "specific lots"}; as of {isoDate(preview.as_of)})
            </span>
          </p>
          <SaleFigures sale={preview.sale} ccy={ccy} />
          <DeltaLine preview={preview} />
          <SoldLotsTable sale={preview.sale} ccy={ccy} />
          {preview.rates_placeholder.length > 0 ? (
            <p className="rounded-lg border border-line bg-surface p-3 text-xs" data-testid="placeholder-rates">
              Placeholder rates, not your rates:{" "}
              {preview.rates_placeholder
                .map((k) => `${RATE_LABEL[k] ?? k} ${pctLabel(preview.rates[k])}`)
                .join(" · ")}
              . Enter your own rates above for an estimate at your rates.
            </p>
          ) : (
            <p className="text-xs text-muted">
              At the rates you entered: federal short-term {pctLabel(preview.rates.short_term)} · federal long-term{" "}
              {pctLabel(preview.rates.long_term)} · state {pctLabel(preview.rates.state)}.
            </p>
          )}
          {preview.history_incomplete ? (
            <p className="text-xs text-muted">
              Some {preview.ticker} shares in scope have imported history that starts mid-position; they can&apos;t
              be dated into lots and are not part of this estimate.
            </p>
          ) : null}
          {preview.n_accounts_excluded > 0 ? (
            <p className="text-xs text-muted">
              Taxable accounts only — {preview.n_accounts_excluded} tax-advantaged account
              {preview.n_accounts_excluded === 1 ? "" : "s"} excluded.
            </p>
          ) : null}
        </div>
      ) : null}

      <p className="mt-3 text-xs text-muted">
        A hypothetical: the tax math of the sale you describe, nothing more. Metron does not choose the holding, the
        quantity, or the lots. The estimate covers this sale alone at flat rates — it leaves out other gains and
        losses in the year, carryforwards, brackets, NIIT and AMT, and shows no tax on a net loss. FIFO here runs
        across the accounts in scope, oldest lot first; brokers relieve lots per account. Amounts are in the
        holding&apos;s own currency. Not a tax filing figure.
      </p>
    </div>
  );
}

export function IfSoldTaxPanel({
  portfolioId,
  tickers,
  accountIds,
  collapsible = false,
}: {
  portfolioId: string;
  /** Held tickers offered as typeahead options (alphabetical) — none is pre-selected. */
  tickers: string[];
  /** The active `?account_id=` selection (empty/omitted = whole portfolio). */
  accountIds?: string[];
  /** Render as a closed-by-default disclosure (the Holdings page). */
  collapsible?: boolean;
}) {
  const body = <PanelBody portfolioId={portfolioId} tickers={tickers} accountIds={accountIds} />;
  if (!collapsible) return body;
  return (
    <CollapsibleSection
      defaultOpen={false}
      className="rounded-lg border border-line bg-surface px-4 py-3"
      summary={
        <span className="text-sm font-medium">
          If sold — tax estimate <span className="font-normal text-muted">· a hypothetical sale you describe</span>
        </span>
      }
    >
      <div className="mt-3">{body}</div>
    </CollapsibleSection>
  );
}
