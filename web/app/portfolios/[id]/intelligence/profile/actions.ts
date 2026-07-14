"use server";

// Server Action: save the investor profile the Intelligence feature compares the portfolio against.
// Runs server-side; revalidates the Intelligence pages so the new targets take effect.

import { revalidatePath } from "next/cache";
import { putAdvisorProfile, MetronApiError, type AdvisorProfile } from "@/lib/api";
import { requireApiAuth } from "@/lib/session";

export type ActionResult = { ok: boolean; message: string };

export async function saveProfileAction(portfolioId: string, profile: AdvisorProfile): Promise<ActionResult> {
  try {
    const apiAuth = await requireApiAuth();
    await putAdvisorProfile(apiAuth, portfolioId, profile);
    revalidatePath(`/portfolios/${portfolioId}/intelligence`);
    revalidatePath(`/portfolios/${portfolioId}/intelligence/profile`);
    return { ok: true, message: "Saved." };
  } catch (e) {
    return { ok: false, message: e instanceof MetronApiError ? e.message : "Save failed — backend reachable?" };
  }
}
