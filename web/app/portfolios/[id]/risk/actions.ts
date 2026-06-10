"use server";

// Server Action for the Risk page: backfill history + compute factor risk. Runs
// server-side (tenant header stays off the browser); revalidates so the result paints.

import { revalidatePath } from "next/cache";
import { computeRisk, MetronApiError } from "@/lib/api";
import { requireTenantId } from "@/lib/session";

export type ActionResult = { ok: boolean; message: string };

export async function computeRiskAction(portfolioId: string): Promise<ActionResult> {
  try {
    const tenantId = await requireTenantId();
    const r = await computeRisk(tenantId, portfolioId);
    revalidatePath(`/portfolios/${portfolioId}/risk`);
    return {
      ok: true,
      message: r.computable ? `Modeled ${r.n_modeled} holdings over ${r.n_obs} days.` : r.reason ?? "Not computable.",
    };
  } catch (e) {
    return { ok: false, message: e instanceof MetronApiError ? e.message : "Compute failed — backend reachable?" };
  }
}
