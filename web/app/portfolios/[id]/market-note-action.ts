"use server";

// Server Action behind the late-loading Holdings market note (Brian, 2026-11-09: slow parts
// "just materialize when they are finished loading"). The streamed render gives the note 8s;
// when it misses that window the client keeps asking here instead of telling the reader to
// reload. Server-side so the tenant credential never reaches the browser.

import { MetronApiError, getMarketNote, type MarketNote } from "@/lib/api";
import { requireApiAuth } from "@/lib/session";

export type MarketNoteResult = { ok: true; note: MarketNote } | { ok: false; retry: boolean };

export async function fetchMarketNoteAction(portfolioId: string, accountIds: string[]): Promise<MarketNoteResult> {
  try {
    const apiAuth = await requireApiAuth();
    return { ok: true, note: await getMarketNote(apiAuth, portfolioId, accountIds) };
  } catch (e) {
    // A 4xx will not change on retry; a timeout or 5xx might.
    const permanent = e instanceof MetronApiError && e.status >= 400 && e.status < 500;
    return { ok: false, retry: !permanent };
  }
}
