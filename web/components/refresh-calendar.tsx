"use client";

// "Refresh earnings" button: re-sources held-ticker earnings dates via the Server
// Action, then revalidation repaints the upcoming-events list.

import { useState, useTransition } from "react";
import { refreshCalendarAction, type ActionResult } from "@/app/portfolios/[id]/calendar/actions";

export function RefreshCalendar({ portfolioId, feedOn }: { portfolioId: string; feedOn?: boolean }) {
  const [pending, start] = useTransition();
  const [result, setResult] = useState<ActionResult | null>(null);

  // Feed-gated (metron-ops#52): earnings dates come from the spine — hide in the beta.
  if (feedOn === false) return null;

  return (
    <div className="flex items-center gap-3">
      <button
        type="button"
        disabled={pending}
        onClick={() => start(async () => setResult(await refreshCalendarAction(portfolioId)))}
        className="shrink-0 rounded border border-line px-3 py-1.5 text-sm font-medium hover:bg-white/5 disabled:opacity-50"
      >
        {pending ? "Refreshing…" : "Refresh earnings"}
      </button>
      {result ? <span className={`text-sm ${result.ok ? "text-muted" : "text-negative"}`}>{result.message}</span> : null}
    </div>
  );
}
