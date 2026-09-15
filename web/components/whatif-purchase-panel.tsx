"use client";

// What-if purchase (metron-ops-I311) — a before/after IMPACT PREVIEW of one hypothetical
// purchase the user describes (ticker + amount or shares). Every number is server-owned:
// concentration, sector/asset-class/account mix, dividend yield on cost, and the tax-lot
// line the purchase would create. Beta/risk is hidden off-feed (no factor-risk compute
// wired for an unentitled build) rather than shown as a fabricated number.

import { useState, useTransition } from "react";
import type { WhatIfPlan, WhatIfSnapshot } from "@/lib/api-planning";
import { fetchWhatIfAction } from "@/app/portfolios/[id]/planning-actions";
import { money, pct1, quantity } from "@/lib/format";
import { Section } from "@/components/ui";

function MixList({ rows }: { rows: WhatIfSnapshot["sector_mix"] }) {
  if (rows.length === 0) return <span className="text-muted">—</span>;
  return (
    <ul className="space-y-0.5">
      {rows.map((r) => (
        <li key={r.key} className="flex justify-between gap-4">
          <span>{r.key}</span>
          <span className="tabular-nums">{pct1(r.weight)}</span>
        </li>
      ))}
    </ul>
  );
}

function SnapshotColumn({ label, snap }: { label: string; snap: WhatIfSnapshot }) {
  return (
    <div className="min-w-0 flex-1 rounded-lg border border-line p-3">
      <div className="text-xs uppercase tracking-wide text-muted">{label}</div>
      <dl className="mt-2 space-y-2 text-sm">
        <div>
          <dt className="text-xs text-muted">Concentration</dt>
          <dd className="tabular-nums">
            HHI {snap.concentration.hhi.toFixed(3)} · effective N {snap.concentration.effective_n.toFixed(1)} · top-5{" "}
            {pct1(snap.concentration.top5_share)} · top-10 {pct1(snap.concentration.top10_share)}
          </dd>
        </div>
        <div>
          <dt className="text-xs text-muted">Sector mix</dt>
          <dd>
            <MixList rows={snap.sector_mix} />
          </dd>
        </div>
        <div>
          <dt className="text-xs text-muted">Asset-class mix</dt>
          <dd>
            <MixList rows={snap.asset_class_mix} />
          </dd>
        </div>
        <div>
          <dt className="text-xs text-muted">Account mix</dt>
          <dd>
            <MixList rows={snap.account_mix} />
          </dd>
        </div>
        <div>
          <dt className="text-xs text-muted">Dividend yield on cost</dt>
          <dd className="tabular-nums">{snap.dividend_yield_on_cost != null ? pct1(snap.dividend_yield_on_cost) : "—"}</dd>
        </div>
      </dl>
    </div>
  );
}

export function WhatIfPurchasePanel({ portfolioId }: { portfolioId: string }) {
  const [symbol, setSymbol] = useState("");
  const [amount, setAmount] = useState("");
  const [userPrice, setUserPrice] = useState("");
  const [plan, setPlan] = useState<WhatIfPlan | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();

  function submit(e: React.FormEvent) {
    e.preventDefault();
    const amt = Number(amount);
    const price = userPrice.trim() ? Number(userPrice) : undefined;
    startTransition(async () => {
      const res = await fetchWhatIfAction(portfolioId, {
        symbol,
        amount_usd: Number.isFinite(amt) && amt > 0 ? amt : undefined,
        user_price: price,
      });
      if (res.ok) {
        setPlan(res.plan);
        setError(null);
      } else {
        setPlan(null);
        setError(res.message);
      }
    });
  }

  return (
    <Section title="What-if purchase" note={plan ? `as of ${plan.as_of}` : undefined}>
      <div className="rounded-lg border border-line p-4">
        <form onSubmit={submit} className="flex flex-wrap items-end gap-3">
          <label className="text-sm">
            <span className="block text-xs uppercase tracking-wide text-muted">Ticker</span>
            <input
              type="text"
              value={symbol}
              onChange={(e) => setSymbol(e.target.value)}
              placeholder="AAPL"
              aria-label="Hypothetical purchase ticker"
              className="mt-1 w-24 rounded border border-line bg-paper px-2 py-1 text-sm uppercase"
            />
          </label>
          <label className="text-sm">
            <span className="block text-xs uppercase tracking-wide text-muted">Amount</span>
            <input
              type="number"
              inputMode="decimal"
              min={0}
              step={100}
              value={amount}
              onChange={(e) => setAmount(e.target.value)}
              placeholder="1000"
              aria-label="Hypothetical purchase amount"
              className="mt-1 w-32 rounded border border-line bg-paper px-2 py-1 text-right text-sm tabular-nums"
            />
          </label>
          <label className="text-sm">
            <span className="block text-xs uppercase tracking-wide text-muted">Price (if not held / no feed)</span>
            <input
              type="number"
              inputMode="decimal"
              min={0}
              step={0.01}
              value={userPrice}
              onChange={(e) => setUserPrice(e.target.value)}
              placeholder="Optional"
              aria-label="Manually entered price"
              className="mt-1 w-28 rounded border border-line bg-paper px-2 py-1 text-right text-sm tabular-nums"
            />
          </label>
          <button
            type="submit"
            disabled={pending || !symbol || !amount}
            className="rounded border border-line px-3 py-1.5 text-sm transition hover:border-muted hover:bg-white/5 disabled:opacity-50"
          >
            {pending ? "Previewing…" : "Preview impact"}
          </button>
        </form>

        {error ? <p className="mt-3 text-sm text-muted">{error}</p> : null}

        {plan ? (
          <div className="mt-4">
            <div className="flex flex-wrap items-baseline gap-x-6 gap-y-1 text-sm tabular-nums">
              <span>
                {quantity(plan.shares)} sh {plan.symbol} @ {money(plan.price, plan.base_currency)} ({plan.price_source})
              </span>
              <span>{money(plan.usd, plan.base_currency)}</span>
            </div>

            <div className="mt-3 flex flex-col gap-3 sm:flex-row">
              <SnapshotColumn label="Before" snap={plan.before} />
              <SnapshotColumn label="After" snap={plan.after} />
            </div>

            <div className="mt-3 rounded-lg border border-line p-3 text-sm">
              <div className="text-xs uppercase tracking-wide text-muted">Tax lot created</div>
              <div className="mt-1 tabular-nums">
                {plan.tax_lot.symbol} — {quantity(plan.tax_lot.shares)} sh @ {money(plan.tax_lot.price, plan.base_currency)},
                cost basis {money(plan.tax_lot.cost_basis, plan.base_currency)}, opened {plan.tax_lot.trade_date}
              </div>
            </div>

            {!plan.beta_available ? (
              <p className="mt-2 text-xs text-muted">
                Beta / risk impact needs the licensed market-data feed — hidden on this build.
              </p>
            ) : null}
          </div>
        ) : null}

        <p className="mt-3 text-xs text-muted">
          A hypothetical impact preview of the purchase you describe. Metron does not choose the ticker or the
          amount.
        </p>
      </div>
    </Section>
  );
}
