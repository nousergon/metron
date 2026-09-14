"use client";

// Deploy cash (metron-ops#300, Brian ruling 2026-09-14) — the Overview panel answering
// "if I have $x to spend today, what should I spend it on?".
//
// A RANKING UNDER CONSTRAINTS, NOT A FORECAST. The technical rating this orders by graded
// IC ~= 0 at 1-20 days (metron-ops#295), so the panel states what it is doing (ordering
// candidates by technical attractiveness, then spending down that order under the user's
// position and sector limits), renders the server's disclaimer verbatim, and links the
// rating's own measured track record so the reader can see the evidence rather than take
// the ordering on trust.
//
// Owner (feed-entitled) build only — the Overview does not render this panel off-feed, and
// the backend 404s independently, so the gate does not depend on the client.
//
// Every number here comes from the server plan. The panel computes nothing: the limits,
// the line sizes, the reasons and the unallocated remainder are all server-owned, so the
// screen can never show a different plan from the one the API produced.

import { useState, useTransition } from "react";
import Link from "next/link";
import type { DeployCashPlan } from "@/lib/api";
import { fetchDeployCashAction } from "@/app/portfolios/[id]/deploy-cash-action";
import { money, moneyWhole, pct1, quantity } from "@/lib/format";
import { Section, Table } from "@/components/ui";

const LABEL_CLASS: Record<string, string> = {
  "Strong Buy": "text-emerald-500",
  Buy: "text-emerald-400",
};

function Limits({ plan }: { plan: DeployCashPlan }) {
  const c = plan.config;
  return (
    <p className="mt-2 text-xs text-muted">
      Limits applied: max {pct1(c.max_position_weight)} per position · max {pct1(c.max_sector_weight)} per sector ·
      minimum line {moneyWhole(c.min_line_usd, plan.base_currency)}
      {c.whole_shares_only ? " · whole shares only" : ""} · eligible ratings {c.eligible_labels.join(", ")}.
    </p>
  );
}

export function DeployCashPanel({ portfolioId }: { portfolioId: string }) {
  const [amount, setAmount] = useState<string>("");
  const [plan, setPlan] = useState<DeployCashPlan | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();

  function submit(e: React.FormEvent) {
    e.preventDefault();
    const value = Number(amount);
    startTransition(async () => {
      const res = await fetchDeployCashAction(portfolioId, value);
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
    <Section title="Deploy cash" note={plan ? `as of ${plan.as_of}` : undefined}>
      <div className="rounded-lg border border-line p-4">
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
              aria-label="Amount to deploy"
              className="mt-1 w-40 rounded border border-line bg-paper px-2 py-1 text-right text-sm tabular-nums"
            />
          </label>
          <button
            type="submit"
            disabled={pending || !amount}
            className="rounded border border-line px-3 py-1.5 text-sm transition hover:border-muted hover:bg-white/5 disabled:opacity-50"
          >
            {pending ? "Ranking…" : "Rank candidates"}
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
              {plan.rating_as_of ? (
                <span className="text-xs text-muted">
                  ratings as of {plan.rating_as_of} ({plan.rating_basis})
                </span>
              ) : null}
            </div>

            {plan.lines.length > 0 ? (
              <div className="mt-3">
                <Table head={["Ticker", "Rating", "Score", "Shares", "Price", "Amount", "Why"]}>
                  {plan.lines.map((line) => (
                    <tr key={line.ticker} className="border-t border-line align-top">
                      <td className="px-3 py-2 font-medium">{line.ticker}</td>
                      <td className={`px-3 py-2 ${LABEL_CLASS[line.technical_label] ?? ""}`}>
                        {line.technical_label}
                      </td>
                      <td className="px-3 py-2 tabular-nums">{line.score.toFixed(2)}</td>
                      <td className="px-3 py-2 tabular-nums">{quantity(line.shares_est)}</td>
                      <td className="px-3 py-2 tabular-nums">{money(line.price, plan.base_currency)}</td>
                      <td className="px-3 py-2 tabular-nums">{money(line.usd, plan.base_currency)}</td>
                      <td className="px-3 py-2 text-xs text-muted">
                        <ul>
                          {line.reasons.map((r) => (
                            <li key={r}>{r}</li>
                          ))}
                        </ul>
                        {line.constraints_hit.length > 0 ? (
                          <div className="mt-1">limited by {line.constraints_hit.join(", ").replaceAll("_", " ")}</div>
                        ) : null}
                      </td>
                    </tr>
                  ))}
                </Table>
              </div>
            ) : (
              <p className="mt-3 text-sm text-muted">No candidate cleared the limits for this amount.</p>
            )}

            {plan.unallocated_usd > 0 && plan.unallocated_reasons.length > 0 ? (
              <div className="mt-3 text-xs text-muted">
                <div>
                  {money(plan.unallocated_usd, plan.base_currency)} left unplaced — cash is never forced into a line:
                </div>
                <ul className="mt-1 list-disc pl-4">
                  {plan.unallocated_reasons.map((r) => (
                    <li key={r}>{r}</li>
                  ))}
                </ul>
              </div>
            ) : null}

            <Limits plan={plan} />
          </div>
        ) : null}

        {/* Verbatim server copy (metron-ops#300 §4) + the measured track record beside it,
            so the ordering is read against its own evidence rather than on trust. */}
        <p className="mt-3 text-xs text-muted">
          {plan?.disclaimer ??
            "Ranked by technical attractiveness under your position and sector limits. Describes recent price action; not investment advice."}{" "}
          <Link href={`/portfolios/${portfolioId}/diagnostics`} className="underline hover:text-ink">
            See this rating&apos;s track record
          </Link>
          .
        </p>
      </div>
    </Section>
  );
}
