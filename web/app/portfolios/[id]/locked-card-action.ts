"use server";

// Server Action for a locked-card tap (metron-ops-I310). Server-side so the session
// credential never reaches the browser. The API counts taps only for a live external-demo
// session and only for a card in its closed set; an owner previewing the page gets a 401
// there, which is correct (owner taps are not demand), so failures are not surfaced.

import { recordLockedCardTap } from "@/lib/api";
import { requireApiAuth } from "@/lib/session";

export async function recordLockedCardTapAction(card: string): Promise<{ ok: boolean }> {
  try {
    await recordLockedCardTap(await requireApiAuth(), card);
    return { ok: true };
  } catch {
    // Counter write refused or backend unreachable: the tap acknowledgement is purely
    // cosmetic, and the missing count is visible as a lower number on /external-demo/counters.
    return { ok: false };
  }
}
