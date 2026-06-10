"use server";

// Server Action for the Attribution page: resolve sectors + backfill history + run
// the Brinson decomposition. Runs server-side (tenant header stays off the browser);
// revalidates so the result paints.

import { revalidatePath } from "next/cache";
import { computeAttribution, MetronApiError } from "@/lib/api";
import { requireTenantId } from "@/lib/session";

export type ActionResult = { ok: boolean; message: string };

export async function computeAttributionAction(portfolioId: string): Promise<ActionResult> {
  try {
    const tenantId = await requireTenantId();
    const a = await computeAttribution(tenantId, portfolioId);
    revalidatePath(`/portfolios/${portfolioId}/attribution`);
    return {
      ok: true,
      message: a.computable
        ? `Attributed ${a.n_sectors} sectors, ${(a.coverage * 100).toFixed(0)}% covered.`
        : a.reason ?? "Not computable.",
    };
  } catch (e) {
    return { ok: false, message: e instanceof MetronApiError ? e.message : "Compute failed — backend reachable?" };
  }
}
