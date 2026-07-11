"use server";

// Server Action for the Calendar page: refresh held-ticker earnings dates, then
// revalidate so the upcoming-events list repaints.

import { revalidatePath } from "next/cache";
import { refreshCalendar, MetronApiError } from "@/lib/api";
import { requireApiAuth } from "@/lib/session";

export type ActionResult = { ok: boolean; message: string };

export async function refreshCalendarAction(portfolioId: string): Promise<ActionResult> {
  try {
    const apiAuth = await requireApiAuth();
    const cal = await refreshCalendar(apiAuth, portfolioId);
    revalidatePath(`/portfolios/${portfolioId}/calendar`);
    return { ok: true, message: `${cal.n_events} upcoming event${cal.n_events === 1 ? "" : "s"}.` };
  } catch (e) {
    return { ok: false, message: e instanceof MetronApiError ? e.message : "Refresh failed — backend reachable?" };
  }
}
