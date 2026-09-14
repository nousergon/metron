"use server";

// Server Action for the Overview "Deploy cash" panel (metron-ops#300): rank a typed amount
// across the portfolio's candidates under the server-declared position/sector limits.
//
// Server-side so the tenant credential never reaches the browser, same as every other
// action here. The backend is the gate: it 404s off a feed-entitled build, so a client that
// somehow calls this on a beta deployment gets a message, not a plan.

import { getDeployCash, MetronApiError, type DeployCashPlan } from "@/lib/api";
import { requireApiAuth } from "@/lib/session";

export type DeployCashResult = { ok: true; plan: DeployCashPlan } | { ok: false; message: string };

export async function fetchDeployCashAction(portfolioId: string, amount: number): Promise<DeployCashResult> {
  if (!Number.isFinite(amount) || amount <= 0) {
    return { ok: false, message: "Enter an amount greater than zero." };
  }
  try {
    const apiAuth = await requireApiAuth();
    return { ok: true, plan: await getDeployCash(apiAuth, portfolioId, amount) };
  } catch (e) {
    if (e instanceof MetronApiError && e.status === 404) {
      return { ok: false, message: "This view isn't available on your plan." };
    }
    return { ok: false, message: "Couldn't build a plan — is the backend reachable?" };
  }
}
