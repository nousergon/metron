"use server";

// Server Actions for the retirement-goal inputs (metron-ops-I316): save the
// user-authored goal, then revalidate both the Settings form and the Overview card
// so they never show a stale (or pre-filled) value.

import { revalidatePath } from "next/cache";
import { putGoal, type Goal } from "@/lib/api-goal";
import { MetronApiError } from "@/lib/api";
import { requireApiAuth } from "@/lib/session";

export type ActionResult = { ok: boolean; message: string };

export async function saveGoalAction(portfolioId: string, goal: Goal): Promise<ActionResult> {
  try {
    const apiAuth = await requireApiAuth();
    await putGoal(apiAuth, portfolioId, goal);
    revalidatePath(`/portfolios/${portfolioId}/settings`);
    revalidatePath(`/portfolios/${portfolioId}/overview`);
    return { ok: true, message: "Goal saved." };
  } catch (e) {
    return { ok: false, message: e instanceof MetronApiError ? e.message : "Save failed — backend reachable?" };
  }
}
