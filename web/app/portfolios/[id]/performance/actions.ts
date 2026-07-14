"use server";

// Server Action for the Performance page: reconstruct NAV history from past prices.
// Runs server-side so the tenant header stays off the browser; revalidates the page
// so the seeded metrics + series render immediately.

import { revalidatePath } from "next/cache";
import { MetronApiError, reconstructPerformance } from "@/lib/api";
import { requireApiAuth } from "@/lib/session";

export type ActionResult = { ok: boolean; message: string };

export async function reconstructAction(portfolioId: string): Promise<ActionResult> {
  try {
    const apiAuth = await requireApiAuth();
    const perf = await reconstructPerformance(apiAuth, portfolioId);
    revalidatePath(`/portfolios/${portfolioId}/performance`);
    return { ok: true, message: `Reconstructed ${perf.n_snapshots} day${perf.n_snapshots === 1 ? "" : "s"} of history.` };
  } catch (e) {
    return { ok: false, message: e instanceof MetronApiError ? e.message : "Reconstruction failed — backend reachable?" };
  }
}
