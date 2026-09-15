// Typed client for GET /portfolios/{id}/market-board (metron-ops-I304, Stage A: Held and
// Watchlist scopes only — the Universe scope moved to Stage B, R5 2026-09-15).
//
// A standalone file (never `web/lib/api.ts`, per the deliverable's binding constraint) so
// this Stage A slice doesn't collide with a sibling agent's concurrent edits to that file.
// Duplicates `api.ts`'s tiny `authHeaders`/`get` shape rather than importing them, because
// neither is exported from there — `apiFetch` and `MetronApiError` are, and are reused
// as-is so every backend call still funnels through the SAME validated-URL / no-store /
// error-shape path the rest of the app uses.

import { apiFetch, MetronApiError } from "@/lib/api";
import { DEMO_TENANT_ID } from "@/lib/demo";

function authHeaders(apiAuth: string): Record<string, string> {
  return apiAuth === DEMO_TENANT_ID ? { "X-Tenant-Id": apiAuth } : { Authorization: `Bearer ${apiAuth}` };
}

export type MarketBoardScope = "held" | "watchlist";

export type MarketBoardRow = {
  symbol: string;
  held: boolean;
  // "Technical attractiveness" (metron-ops#302 wording) — the rating label, "Strong Sell"
  // … "Strong Buy", or null when the symbol has no rating yet.
  label: string | null;
  score: number | null; // signed [-1, +1]
  ma_score: number | null;
  osc_score: number | null;
  change_1d_pct: number | null;
  change_5d_pct: number | null;
  basis: "intraday" | "eod" | null;
  as_of: string | null; // per-row — the rating's own as-of, not a board-level timestamp
};

export type MarketBoardTrackRecord = {
  segment: string;
  window: number;
  horizon: number;
  ic_mean: number | null;
  noise_floor_ic: number | null;
  as_of_utc: string | null;
};

export type MarketBoard = {
  scope: MarketBoardScope;
  rows: MarketBoardRow[];
  track_record: MarketBoardTrackRecord | null;
};

export async function getMarketBoard(
  apiAuth: string,
  portfolioId: string,
  scope: MarketBoardScope,
): Promise<MarketBoard> {
  const res = await apiFetch(
    `/portfolios/${encodeURIComponent(portfolioId)}/market-board?scope=${encodeURIComponent(scope)}`,
    { headers: authHeaders(apiAuth), cache: "no-store" },
  );
  if (!res.ok) {
    throw new MetronApiError(res.status, `GET market-board (${scope}) → ${res.status}`);
  }
  return res.json() as Promise<MarketBoard>;
}
