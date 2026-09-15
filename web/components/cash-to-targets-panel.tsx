"use client";

// New cash to my targets (metron-ops-I311) — the panel answering "if I have $x, what
// whole-share purchases close the gap to the targets I set?". Every number is server-owned
// arithmetic against the user's own saved targets; the panel computes nothing and never
// orders rows by anything but the user's own target list.

import { useState, useTransition } from "react";
import type { CashToTargetsPlan } from "@/lib/api-planning";
import { fetchCashToTargetsAction } from "@/app/portfolios/[id]/planning-actions";
import { money, moneyWhole, pct1, quantity } from "@/lib/format";
import { Section, Table } from "@/components/ui";

export function CashToTargetsPanel({ portfolioId, hasTargets }: { portfolioId: string; hasTargets: boolean }) {
  const [amount, setAmount] = useState<string>("");
  const [plan, setPlan] = useState<CashToTargetsPlan | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();

  function submit(e: React.FormEvent) {
    e.preventDefault();
    const value = Number(amount);
    startTransition(async () => {
      const res = await fetchCashToTargetsAction(portfolioId, value);
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
    <Section title="New cash to my targets" note={plan ? `as of ${plan.as_of}` : undefined}>
      <div className="rounded-lg border border-line p-4">
        {!hasTargets ? (
          <p className="text-sm text-muted">
            Set your targets above first — this panel is arithmetic against the targets you set, and has nothing to
            run against until you type some in.
          </p>
        ) : (
          <>
            <form onSubmit={submit} className="flex flex-wrap items-end gap-3">
              <label className="text-sm">
                <span className="block text-xs uppercase tracking-wide text-muted">Amount to deploy</span>
                <input
                  type="number"
                  inputMode="decimal"
                  min={0}
                  step={100}
                  value={amount}
                  onChange={(e) => setAmount(e.target.value)}
                  placeholder="10000"
                  aria-label="Amount to deploy against your targets"
                  className="mt-1 w-40 rounded border border-line bg-paper px-2 py-1 text-right text-sm tabular-nums"
                />
              </label>
              <button
                type="submit"
                disabled={pending || !amount}
                className="rounded border border-line px-3 py-1.5 text-sm transition hover:border-muted hover:bg-white/5 disabled:opacity-50"
              >
                {pending ? "Computing…" : "Compute purchases"}
              </button>
            </form>

            {error ? <p className="mt-3 text-sm text-muted">{error}</p> : null}

            {plan ? (
              <div className="mt-4">
                <div className="flex flex-wrap items-baseline gap-x-6 gap-y-1 text-sm tabular-nums">
                  <span>
                    <span className="text-muted">Allocated </span>
                    {money(plan.allocated_usd, plan.base_currency)}
                  </span>
                  <span>
                    <span className="text-muted">Left over </span>
                    {money(plan.unallocated_usd, plan.base_currency)}
                  </span>
                </div>

                {plan.lines.length > 0 ? (
                  <div className="mt-3">
                    <Table head={["Symbol", "Target", "Weight before", "Weight after", "Shares", "Price", "Amount"]}>
                      {plan.lines.map((line) => (
                        <tr key={line.symbol} className="border-t border-line align-top">
                          <td className="px-3 py-2 font-medium">{line.symbol}</td>
                          <td className="px-3 py-2 tabular-nums">{pct1(line.target_weight)}</td>
                          <td className="px-3 py-2 tabular-nums">{pct1(line.weight_before)}</td>
                          <td className="px-3 py-2 tabular-nums">{pct1(line.weight_after)}</td>
                          <td className="px-3 py-2 tabular-nums">{quantity(line.shares)}</td>
                          <td className="px-3 py-2 tabular-nums">{money(line.price, plan.base_currency)}</td>
                          <td className="px-3 py-2 tabular-nums">{money(line.usd, plan.base_currency)}</td>
                        </tr>
                      ))}
                    </Table>
                  </div>
                ) : (
                  <p className="mt-3 text-sm text-muted">No target could take a whole-share purchase for this amount.</p>
                )}

                {plan.unallocated_usd > 0 && plan.unallocated_reasons.length > 0 ? (
                  <div className="mt-3 text-xs text-muted">
                    <div>
                      {money(plan.unallocated_usd, plan.base_currency)} left unplaced — cash is never forced into a
                      line:
                    </div>
                    <ul className="mt-1 list-disc pl-4">
                      {plan.unallocated_reasons.map((r) => (
                        <li key={r}>{r}</li>
                      ))}
                    </ul>
                  </div>
                ) : null}

                <p className="mt-2 text-xs text-muted">
                  Limits applied: {plan.max_single_position != null ? `max ${pct1(plan.max_single_position)} per position · ` : ""}
                  minimum line {moneyWhole(plan.min_line_usd, plan.base_currency)}.
                </p>
              </div>
            ) : null}
          </>
        )}

        {/* Verbatim server copy — the doctrine layer-2 line, rendered unchanged. */}
        <p className="mt-3 text-xs text-muted">{plan?.disclaimer ?? "Arithmetic against the targets you set. Metron does not choose securities."}</p>
      </div>
    </Section>
  );
}
