// Typed server-side client for the plan-targets surface (metron-ops-I311):
// "New cash to my targets" + "What-if purchase". A separate module from lib/api.ts
// (rather than an addition to it) so the L1-comparable surface — arithmetic against a
// table the user typed in, never a Metron-chosen allocation — has its own boundary, per
// the build instructions for this issue.
//
// Mirrors lib/api.ts's request shape (apiFetch's pinned-origin guard, the demo-tenant vs.
// bearer-JWT credential branch, MetronApiError) rather than reusing its private helpers,
// which aren't exported.

import { apiFetch, cacheIdentity, MetronApiError } from "@/lib/api";
import { DEMO_TENANT_ID } from "@/lib/demo";

export { MetronApiError, cacheIdentity };

function authHeaders(apiAuth: string): Record<string, string> {
  return apiAuth === DEMO_TENANT_ID ? { "X-Tenant-Id": apiAuth } : { Authorization: `Bearer ${apiAuth}` };
}

async function readDetail(res: Response, fallback: string): Promise<string> {
  try {
    return ((await res.json()) as { detail?: string }).detail ?? fallback;
  } catch {
    return fallback;
  }
}

async function getJson<T>(apiAuth: string, path: string): Promise<T> {
  const res = await apiFetch(path, { headers: authHeaders(apiAuth), cache: "no-store" });
  if (!res.ok) throw new MetronApiError(res.status, await readDetail(res, `GET ${path} → ${res.status}`));
  return res.json() as Promise<T>;
}

async function writeJson<T>(apiAuth: string, path: string, method: "PUT" | "POST", body: unknown): Promise<T> {
  const res = await apiFetch(path, {
    method,
    headers: { ...authHeaders(apiAuth), "Content-Type": "application/json" },
    body: JSON.stringify(body),
    cache: "no-store",
  });
  if (!res.ok) throw new MetronApiError(res.status, await readDetail(res, `${method} ${path} → ${res.status}`));
  return res.json() as Promise<T>;
}

// ── Plan targets ─────────────────────────────────────────────────────────────────
export type TargetLine = { symbol: string; weight: number };

export type PlanTargets = {
  targets: TargetLine[];
  max_single_position: number | null;
  min_line_usd: number | null;
  available: boolean;
  reason: string | null;
  required_tier: string | null;
};

export const getPlanTargets = (apiAuth: string, portfolioId: string) =>
  getJson<PlanTargets>(apiAuth, `/portfolios/${portfolioId}/plan/targets`);

export type PlanTargetsInput = {
  targets: TargetLine[];
  max_single_position: number | null;
  min_line_usd: number | null;
};

export const putPlanTargets = (apiAuth: string, portfolioId: string, body: PlanTargetsInput) =>
  writeJson<PlanTargets>(apiAuth, `/portfolios/${portfolioId}/plan/targets`, "PUT", body);

// ── New cash to my targets ──────────────────────────────────────────────────────
export type CashToTargetsLine = {
  symbol: string;
  target_weight: number;
  weight_before: number;
  weight_after: number;
  shares: number;
  usd: number;
  price: number;
  price_as_of: string | null;
};

export type CashToTargetsPlan = {
  as_of: string;
  amount_usd: number;
  allocated_usd: number;
  unallocated_usd: number;
  unallocated_reasons: string[];
  lines: CashToTargetsLine[];
  portfolio_value: number;
  deployment_basis: number;
  base_currency: string;
  max_single_position: number | null;
  min_line_usd: number;
  disclaimer: string;
  available: boolean;
  reason: string | null;
  required_tier: string | null;
};

export const postCashToTargets = (apiAuth: string, portfolioId: string, amountUsd: number) =>
  writeJson<CashToTargetsPlan>(apiAuth, `/portfolios/${portfolioId}/plan/cash-to-targets`, "POST", {
    amount_usd: amountUsd,
  });

// ── What-if purchase ────────────────────────────────────────────────────────────
export type Concentration = {
  n_positions: number;
  hhi: number;
  effective_n: number;
  top5_share: number;
  top10_share: number;
  max_position_ticker: string | null;
  max_position_weight: number;
};

export type MixRow = { key: string; weight: number };

export type WhatIfSnapshot = {
  concentration: Concentration;
  sector_mix: MixRow[];
  asset_class_mix: MixRow[];
  account_mix: MixRow[];
  dividend_yield_on_cost: number | null;
};

export type TaxLotPreview = {
  symbol: string;
  shares: number;
  price: number;
  trade_date: string;
  cost_basis: number;
};

export type WhatIfPlan = {
  as_of: string;
  symbol: string;
  shares: number;
  usd: number;
  price: number;
  price_source: "held" | "spine" | "user_entered";
  price_as_of: string | null;
  before: WhatIfSnapshot;
  after: WhatIfSnapshot;
  beta_available: boolean;
  tax_lot: TaxLotPreview;
  portfolio_value: number;
  base_currency: string;
};

export type WhatIfInput = {
  symbol: string;
  amount_usd?: number;
  shares?: number;
  user_price?: number;
  account_label?: string;
};

export const postWhatIf = (apiAuth: string, portfolioId: string, body: WhatIfInput) =>
  writeJson<WhatIfPlan>(apiAuth, `/portfolios/${portfolioId}/plan/whatif`, "POST", body);
