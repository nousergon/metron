"use client";

// Retirement-goal inputs (metron-ops-I316) — the Settings-page editor. Every field
// starts EMPTY when no goal is saved yet: no defaults, no pre-fill. Doctrine layer 2
// exemption (docs/intelligence-doctrine.md, positioning §3c.2) holds only because
// these are numbers the user typed — Metron never suggests a target, a date, a
// contribution, or a withdrawal rate, so this form must never seed one.

import { useState, useTransition } from "react";
import { saveGoalAction } from "@/app/portfolios/[id]/goal-actions";
import type { Goal } from "@/lib/api-goal";

function Status({ msg }: { msg: { ok: boolean; text: string } | null }) {
  if (!msg) return null;
  return <span className={`text-sm ${msg.ok ? "text-positive" : "text-negative"}`}>{msg.text}</span>;
}

/** ``value`` in fraction form (e.g. 0.04) -> the percent string the input shows
 * (e.g. "4"); null/undefined stay blank rather than becoming "0". */
function toPercentInput(value: number | null | undefined): string {
  // Round off binary floating-point noise (0.035 * 100 → 3.5000000000000004)
  // before it reaches the input — a round-tripped value should look untouched.
  return value == null ? "" : String(Math.round(value * 100 * 1e6) / 1e6);
}

function fromPercentInput(text: string): number | null {
  const trimmed = text.trim();
  if (!trimmed) return null;
  const n = Number(trimmed);
  return Number.isFinite(n) ? n / 100 : null;
}

function toAmountInput(value: number | null | undefined): string {
  return value == null ? "" : String(value);
}

function fromAmountInput(text: string): number | null {
  const trimmed = text.trim();
  if (!trimmed) return null;
  const n = Number(trimmed);
  return Number.isFinite(n) ? n : null;
}

export function GoalForm({ portfolioId, current }: { portfolioId: string; current: Goal }) {
  const [targetAmount, setTargetAmount] = useState(toAmountInput(current.target_amount_usd));
  const [targetDate, setTargetDate] = useState(current.target_date ?? "");
  const [contribution, setContribution] = useState(toAmountInput(current.annual_contribution_usd));
  const [withdrawalRate, setWithdrawalRate] = useState(toPercentInput(current.withdrawal_rate));
  const [pending, start] = useTransition();
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  function save() {
    setMsg(null);
    start(async () => {
      const goal: Goal = {
        target_amount_usd: fromAmountInput(targetAmount),
        target_date: targetDate.trim() || null,
        annual_contribution_usd: fromAmountInput(contribution),
        withdrawal_rate: fromPercentInput(withdrawalRate),
      };
      const r = await saveGoalAction(portfolioId, goal);
      setMsg({ ok: r.ok, text: r.message });
    });
  }

  return (
    <div className="max-w-xl space-y-3">
      <p className="text-xs text-muted">
        Arithmetic against the goal you set. Describes your portfolio; not a plan or advice.
      </p>
      <label className="block text-sm">
        <span className="text-muted">Retirement number (USD)</span>
        <input
          type="number"
          inputMode="decimal"
          className="mt-1 block w-full rounded border border-line px-2 py-1"
          value={targetAmount}
          onChange={(e) => setTargetAmount(e.target.value)}
          placeholder="e.g. 1500000"
        />
      </label>
      <label className="block text-sm">
        <span className="text-muted">Target date</span>
        <input
          type="date"
          className="mt-1 block w-full rounded border border-line px-2 py-1"
          value={targetDate}
          onChange={(e) => setTargetDate(e.target.value)}
        />
      </label>
      <label className="block text-sm">
        <span className="text-muted">Planned annual contribution (USD)</span>
        <input
          type="number"
          inputMode="decimal"
          className="mt-1 block w-full rounded border border-line px-2 py-1"
          value={contribution}
          onChange={(e) => setContribution(e.target.value)}
          placeholder="e.g. 25000"
        />
      </label>
      <label className="block text-sm">
        <span className="text-muted">Planned withdrawal rate (%)</span>
        <input
          type="number"
          inputMode="decimal"
          step="0.1"
          className="mt-1 block w-full rounded border border-line px-2 py-1"
          value={withdrawalRate}
          onChange={(e) => setWithdrawalRate(e.target.value)}
          placeholder="e.g. 4"
        />
      </label>
      <div className="flex items-center gap-2">
        <button
          type="button"
          disabled={pending}
          onClick={save}
          className="rounded bg-ink px-3 py-1 text-sm font-medium text-paper hover:bg-white disabled:opacity-50"
        >
          {pending ? "Saving…" : "Save goal"}
        </button>
        <Status msg={msg} />
      </div>
    </div>
  );
}
