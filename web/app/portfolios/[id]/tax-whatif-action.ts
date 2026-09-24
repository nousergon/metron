"use server";

// Server Action for the "if sold" tax preview (metron-ops#208). Server-side so the tenant
// credential never reaches the browser, same as every other action here. Read-only.

import { getIfSold, MetronApiError, type IfSoldInput, type IfSoldPreview } from "@/lib/api-tax-whatif";
import { requireApiAuth } from "@/lib/session";

export type IfSoldResult = { ok: true; preview: IfSoldPreview } | { ok: false; message: string };

export async function fetchIfSoldAction(portfolioId: string, input: IfSoldInput): Promise<IfSoldResult> {
  if (!input.ticker.trim()) return { ok: false, message: "Enter a ticker." };
  try {
    const apiAuth = await requireApiAuth();
    return { ok: true, preview: await getIfSold(apiAuth, portfolioId, input) };
  } catch (e) {
    if (e instanceof MetronApiError && e.status === 422) {
      // A domain 422 carries a readable detail; a request-validation 422 does not.
      const readable = e.message && !e.message.startsWith("GET ");
      return { ok: false, message: readable ? e.message : "Check the inputs — they must be positive numbers." };
    }
    if (e instanceof MetronApiError && e.status === 404) {
      return { ok: false, message: "This view isn't available on your plan." };
    }
    return { ok: false, message: "Couldn't build the estimate — is the backend reachable?" };
  }
}
