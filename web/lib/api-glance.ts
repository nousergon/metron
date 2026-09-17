// Glance screen client types + fetcher (metron-ops#248 Stage A / #250). One network fetch
// renders the whole screen: GET /portfolios/{id}/glance composes every zone server-side.

import { apiFetch, MetronApiError } from "@/lib/api";
import { DEMO_TENANT_ID } from "@/lib/demo";

export type GlanceState = "pre_open" | "open" | "post_close";
/** Per-row provenance — this surface reports current state, so it is never page-wide. */
export type GlanceProvenance = "live" | "settled" | "as_of_sync" | "as_of_close";

export type GlanceHeadline = {
  base_currency: string;
  total_value: number | null;
  market_value: number | null;
  cash: number | null;
  value_as_of: string | null;
  value_provenance: GlanceProvenance;
  last_sync: string | null;
  day_change: number | null;
  day_pct: number | null;
  day_as_of: string | null;
  day_provenance: GlanceProvenance | null;
  day_note: string | null;
  n_accounts: number;
  n_brokers: number;
  surface: string;
};

export type GlancePathPoint = { date: string; nav: number };
export type GlanceIntradayPathPoint = { as_of: string; nav: number };

export type GlancePath = {
  available: boolean;
  reason: string | null;
  points: GlancePathPoint[];
  as_of: string | null;
  provenance: GlanceProvenance;
  state: GlanceState;
  /** Open-state intraday sparkline (metron-ops-I324) — populated only when the render
   *  state is "open" AND a live series is reachable; every point in it is "live"
   *  provenance by construction, never mixed with the settled `points` above. */
  intraday_points: GlanceIntradayPathPoint[];
  surface: string;
};

export type GlanceItem = {
  facet_key: string;
  family: string;
  label: string;
  text: string;
  as_of: string;
  provenance: GlanceProvenance;
  surface: string;
  score: number;
  ticker: string | null;
  amount: number | null;
  pct: number | null;
  event_date: string | null;
};

export type GlanceRankedZone = {
  key: "movers" | "insights" | "ahead";
  items: GlanceItem[];
  is_floor: boolean;
  floor_text: string | null;
  as_of: string;
  unavailable: string[];
};

export type GlanceIntegrity = {
  status: "reconciled" | "stale" | "breaks" | "never" | "ledger_only" | "no_accounts";
  healthy: boolean;
  text: string;
  open_breaks: number;
  last_reconciled_at: string | null;
  positions_as_of: string | null;
  brokers: string[];
  as_of: string;
  surface: string;
};

export type Glance = {
  portfolio_id: string;
  generated_at: string;
  state: GlanceState;
  tier: string;
  feed_enabled: boolean;
  headline: GlanceHeadline;
  path: GlancePath;
  movers: GlanceRankedZone;
  insights: GlanceRankedZone;
  ahead: GlanceRankedZone;
  integrity: GlanceIntegrity;
  coverage: { candidate_facets: number; produced_facets: number; observations: number };
  degraded: string[];
  timings_ms: Record<string, number>;
};

/** Mirrors lib/api.ts's private auth-header branch: the demo credential IS the demo tenant id. */
function authHeaders(apiAuth: string): Record<string, string> {
  return apiAuth === DEMO_TENANT_ID ? { "X-Tenant-Id": apiAuth } : { Authorization: `Bearer ${apiAuth}` };
}

/** The whole glance payload. `preview` (owner tier simulator) is forwarded as X-Preview-*
 *  headers and honoured server-side only when the simulator is on. */
export async function getGlance(
  apiAuth: string,
  portfolioId: string,
  preview?: { tier?: string; feed?: boolean },
): Promise<Glance> {
  const headers: Record<string, string> = { ...authHeaders(apiAuth) };
  if (preview?.tier) headers["X-Preview-Tier"] = preview.tier;
  if (preview?.feed !== undefined) headers["X-Preview-Feed"] = String(preview.feed);
  const path = `/portfolios/${encodeURIComponent(portfolioId)}/glance`;
  const res = await apiFetch(path, { headers, cache: "no-store" });
  if (!res.ok) throw new MetronApiError(res.status, `GET ${path} → ${res.status}`);
  return res.json() as Promise<Glance>;
}
