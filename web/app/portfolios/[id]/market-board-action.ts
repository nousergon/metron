"use server";

// Server Action for the Market board's Held/Watchlist toggle (metron-ops-I304): re-fetch
// the board for the tapped scope without a full page reload. Server-side so the tenant
// credential never reaches the browser, same as every other action here. The backend is
// the real gate: it 404s off a feed-entitled build, so a client that somehow calls this on
// a beta deployment gets a message, never a fabricated board.

import { getMarketBoard, type MarketBoard, type MarketBoardScope } from "@/lib/api-market-board";
import { MetronApiError } from "@/lib/api";
import { requireApiAuth } from "@/lib/session";

export type MarketBoardResult = { ok: true; board: MarketBoard } | { ok: false; message: string };

export async function fetchMarketBoardAction(
  portfolioId: string,
  scope: MarketBoardScope,
): Promise<MarketBoardResult> {
  try {
    const apiAuth = await requireApiAuth();
    return { ok: true, board: await getMarketBoard(apiAuth, portfolioId, scope) };
  } catch (e) {
    if (e instanceof MetronApiError && e.status === 404) {
      return { ok: false, message: "This view isn't available on your plan." };
    }
    return { ok: false, message: "Couldn't load the market board — is the backend reachable?" };
  }
}
