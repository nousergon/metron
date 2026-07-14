"use client";

// Investor-profile editor for Intelligence. Collects the stated targets, builds an
// AdvisorProfile, and saves via the Server Action. Targets are entered as human units
// (percent / dollars) and converted to the fractions/USD the backend stores.
//
// Pre-registration compliance wall (metron-ops#164, metron-ops#166): the SUITABILITY
// inputs (strategy, risk_tolerance, time_horizon) are HIDDEN — under the impersonal
// lane, tailoring generated output on suitability is the personalization trigger, so
// pre-registration we don't collect them. The fields stay in the AdvisorProfile
// type/API contract and any stored values are passed through UNCHANGED on save (a
// targets edit must never wipe them). Restoring the inputs post-registration is a
// deliberate, findable change here; the backend wall lives in metron-ops
// (llm.PRE_REGISTRATION_EXCLUDED_PROFILE_FIELDS).

import { useState, useTransition } from "react";
import { saveProfileAction, type ActionResult } from "@/app/portfolios/[id]/intelligence/profile/actions";
import type { AdvisorProfile } from "@/lib/api";

const FIELD = "block text-sm";
const INPUT = "mt-1 w-full rounded border border-line px-2 py-1.5 text-sm";

function pctToFraction(v: string): number | null {
  const n = Number(v);
  return Number.isFinite(n) && v.trim() !== "" ? n / 100 : null;
}

function csv(v: string): string[] {
  return v.split(",").map((s) => s.trim()).filter(Boolean);
}

export function AdvisorProfileForm({ portfolioId, initial }: { portfolioId: string; initial: AdvisorProfile }) {
  const [pending, start] = useTransition();
  const [result, setResult] = useState<ActionResult | null>(null);

  const [rebalance, setRebalance] = useState(initial.rebalance_frequency);
  const [usEquity, setUsEquity] = useState(initial.target_allocation.us_equity != null ? String(initial.target_allocation.us_equity * 100) : "");
  const [intl, setIntl] = useState(initial.target_allocation.international != null ? String(initial.target_allocation.international * 100) : "");
  const [maxPos, setMaxPos] = useState(initial.max_single_position != null ? String(initial.max_single_position * 100) : "");
  const [incomeTarget, setIncomeTarget] = useState(initial.income_target != null ? String(initial.income_target) : "");
  const [overweight, setOverweight] = useState(initial.overweight_sectors.join(", "));
  const [avoid, setAvoid] = useState(initial.avoid_sectors.join(", "));

  function submit() {
    const targetAllocation: Record<string, number> = {};
    const us = pctToFraction(usEquity);
    const it = pctToFraction(intl);
    if (us != null) targetAllocation.us_equity = us;
    if (it != null) targetAllocation.international = it;

    const profile: AdvisorProfile = {
      // Suitability fields: no inputs pre-registration (metron-ops#166) — pass the
      // stored values through unchanged so saving targets never mutates them.
      strategy: initial.strategy,
      risk_tolerance: initial.risk_tolerance,
      time_horizon: initial.time_horizon,
      target_allocation: targetAllocation,
      overweight_sectors: csv(overweight),
      avoid_sectors: csv(avoid),
      income_target: incomeTarget.trim() !== "" && Number.isFinite(Number(incomeTarget)) ? Number(incomeTarget) : null,
      max_single_position: pctToFraction(maxPos),
      rebalance_frequency: rebalance,
    };
    start(async () => setResult(await saveProfileAction(portfolioId, profile)));
  }

  return (
    <div className="max-w-xl space-y-4">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <label className={FIELD}>
          Rebalance frequency
          <input className={INPUT} value={rebalance} onChange={(e) => setRebalance(e.target.value)} placeholder="e.g. annually" />
        </label>
        <label className={FIELD}>
          US equity target (%)
          <input className={INPUT} type="number" value={usEquity} onChange={(e) => setUsEquity(e.target.value)} placeholder="60" />
        </label>
        <label className={FIELD}>
          International target (%)
          <input className={INPUT} type="number" value={intl} onChange={(e) => setIntl(e.target.value)} placeholder="20" />
        </label>
        <label className={FIELD}>
          Max single position (%)
          <input className={INPUT} type="number" value={maxPos} onChange={(e) => setMaxPos(e.target.value)} placeholder="10" />
        </label>
        <label className={FIELD}>
          Annual income target ($)
          <input className={INPUT} type="number" value={incomeTarget} onChange={(e) => setIncomeTarget(e.target.value)} placeholder="5000" />
        </label>
      </div>
      <label className={FIELD}>
        Preferred sectors (comma-separated)
        <input className={INPUT} value={overweight} onChange={(e) => setOverweight(e.target.value)} placeholder="Health Care, Information Technology" />
      </label>
      <label className={FIELD}>
        Sectors to avoid (comma-separated)
        <input className={INPUT} value={avoid} onChange={(e) => setAvoid(e.target.value)} placeholder="Energy" />
      </label>

      <div className="flex items-center gap-3">
        <button
          type="button"
          disabled={pending}
          onClick={submit}
          className="rounded border border-line px-3 py-1.5 text-sm font-medium hover:bg-white/5 disabled:opacity-50"
        >
          {pending ? "Saving…" : "Save profile"}
        </button>
        {result ? <span className={`text-sm ${result.ok ? "text-muted" : "text-negative"}`}>{result.message}</span> : null}
      </div>
    </div>
  );
}
