"use server";

// Server Action for the Intelligence page: run the Claude narrative for the current state.
// Runs server-side (tenant header stays off the browser); revalidates so the fresh
// commentary paints. The generate call is the one paid path — gated behind a click.

import { revalidatePath } from "next/cache";
import { generateAdvisor, MetronApiError } from "@/lib/api";
import { requireApiAuth } from "@/lib/session";

export type ActionResult = { ok: boolean; message: string };

export async function generateAdvisorAction(portfolioId: string): Promise<ActionResult> {
  try {
    const apiAuth = await requireApiAuth();
    await generateAdvisor(apiAuth, portfolioId);
    revalidatePath(`/portfolios/${portfolioId}/intelligence`);
    return { ok: true, message: "Generated." };
  } catch (e) {
    // The backend returns 503 with a reason (e.g. missing ANTHROPIC_API_KEY) — surface it.
    return { ok: false, message: e instanceof MetronApiError ? e.message : "Generation failed — backend reachable?" };
  }
}
