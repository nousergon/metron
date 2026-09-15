// Typed server-side client for the retirement-goal endpoints (metron-ops-I316).
//
// A SEPARATE file from lib/api.ts on purpose (the issue's instruction, and it avoids
// touching that file's ownership boundary): `authHeaders`/`get<T>` there are module-
// private, so this file re-derives the same tiny auth-header branch rather than
// importing something unexported. Runs in Server Components / Server Actions only,
// exactly like lib/api.ts.

import { DEMO_TENANT_ID } from "@/lib/demo";
import { apiFetch, MetronApiError } from "@/lib/api";

function authHeaders(apiAuth: string): Record<string, string> {
  return apiAuth === DEMO_TENANT_ID ? { "X-Tenant-Id": apiAuth } : { Authorization: `Bearer ${apiAuth}` };
}

export type Goal = {
  target_amount_usd: number | null;
  target_date: string | null; // "YYYY-MM-DD"
  annual_contribution_usd: number | null;
  withdrawal_rate: number | null; // fraction, e.g. 0.04
};

export const EMPTY_GOAL: Goal = {
  target_amount_usd: null,
  target_date: null,
  annual_contribution_usd: null,
  withdrawal_rate: null,
};

/** One computed goal facet's envelope — every facet in GoalFacets shares this shape.
 * `available: false` carries a `reason` (no_goal_set / no_valuation / insufficient_history
 * / expense_ratio_source_not_provisioned / ...); the caller renders the empty state from
 * that, never from a thrown error. */
export type GoalFacetEnvelope = {
  available: boolean;
  as_of: string;
  reason?: string;
  [key: string]: unknown;
};

export type GoalFacets = {
  goal_progress: GoalFacetEnvelope;
  goal_trajectory_range: GoalFacetEnvelope;
  goal_timing_cost: GoalFacetEnvelope;
  goal_fee_drag: GoalFacetEnvelope;
  goal_asset_location_drag: GoalFacetEnvelope;
  goal_withdrawal_readiness: GoalFacetEnvelope;
};

export async function getGoal(apiAuth: string, portfolioId: string): Promise<Goal> {
  const res = await apiFetch(`/portfolios/${portfolioId}/goal`, {
    headers: authHeaders(apiAuth),
    cache: "no-store",
  });
  if (!res.ok) {
    throw new MetronApiError(res.status, `GET goal → ${res.status}`);
  }
  return res.json() as Promise<Goal>;
}

export async function getGoalFacets(apiAuth: string, portfolioId: string): Promise<GoalFacets> {
  const res = await apiFetch(`/portfolios/${portfolioId}/goal/facets`, {
    headers: authHeaders(apiAuth),
    cache: "no-store",
  });
  if (!res.ok) {
    throw new MetronApiError(res.status, `GET goal facets → ${res.status}`);
  }
  return res.json() as Promise<GoalFacets>;
}

/** Create or update the portfolio's goal (PUT, idempotent — full replace, matching
 * `putPreferences`'s convention). No field is ever pre-filled by this client: the
 * caller passes exactly what the user typed, nulls included. */
export async function putGoal(apiAuth: string, portfolioId: string, goal: Goal): Promise<Goal> {
  const res = await apiFetch(`/portfolios/${portfolioId}/goal`, {
    method: "PUT",
    headers: { ...authHeaders(apiAuth), "Content-Type": "application/json" },
    body: JSON.stringify(goal),
    cache: "no-store",
  });
  if (!res.ok) {
    throw new MetronApiError(res.status, `save goal → ${res.status}`);
  }
  return res.json() as Promise<Goal>;
}
