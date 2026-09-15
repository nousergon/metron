"use server";

// Server Actions for the Plan page (metron-ops-I311): save the user's own targets, run
// "New cash to my targets" against them, and run "What-if purchase". Server-side so the
// tenant credential never reaches the browser, same as every other action here.

import {
  getPlanTargets,
  MetronApiError,
  postCashToTargets,
  postWhatIf,
  putPlanTargets,
  type CashToTargetsPlan,
  type PlanTargets,
  type PlanTargetsInput,
  type WhatIfInput,
  type WhatIfPlan,
} from "@/lib/api-planning";
import { requireApiAuth } from "@/lib/session";

export type PlanTargetsResult = { ok: true; targets: PlanTargets } | { ok: false; message: string };
export type CashToTargetsResult = { ok: true; plan: CashToTargetsPlan } | { ok: false; message: string };
export type WhatIfResult = { ok: true; plan: WhatIfPlan } | { ok: false; message: string };

function errorMessage(e: unknown, fallback: string): string {
  if (e instanceof MetronApiError) return e.message || fallback;
  return fallback;
}

export async function fetchPlanTargetsAction(portfolioId: string): Promise<PlanTargetsResult> {
  try {
    const apiAuth = await requireApiAuth();
    return { ok: true, targets: await getPlanTargets(apiAuth, portfolioId) };
  } catch (e) {
    return { ok: false, message: errorMessage(e, "Couldn't load your targets.") };
  }
}

export async function savePlanTargetsAction(
  portfolioId: string,
  body: PlanTargetsInput,
): Promise<PlanTargetsResult> {
  try {
    const apiAuth = await requireApiAuth();
    return { ok: true, targets: await putPlanTargets(apiAuth, portfolioId, body) };
  } catch (e) {
    return { ok: false, message: errorMessage(e, "Couldn't save your targets.") };
  }
}

export async function fetchCashToTargetsAction(portfolioId: string, amount: number): Promise<CashToTargetsResult> {
  if (!Number.isFinite(amount) || amount <= 0) {
    return { ok: false, message: "Enter an amount greater than zero." };
  }
  try {
    const apiAuth = await requireApiAuth();
    return { ok: true, plan: await postCashToTargets(apiAuth, portfolioId, amount) };
  } catch (e) {
    if (e instanceof MetronApiError && e.status === 422) {
      return { ok: false, message: "Set your targets first." };
    }
    return { ok: false, message: errorMessage(e, "Couldn't build a plan — is the backend reachable?") };
  }
}

export async function fetchWhatIfAction(portfolioId: string, body: WhatIfInput): Promise<WhatIfResult> {
  try {
    const apiAuth = await requireApiAuth();
    return { ok: true, plan: await postWhatIf(apiAuth, portfolioId, body) };
  } catch (e) {
    return { ok: false, message: errorMessage(e, "Couldn't build a what-if preview.") };
  }
}
