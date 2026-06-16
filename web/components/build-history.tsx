"use client";

// "Build history" button: reconstructs NAV history from past prices via the Server
// Action, then revalidation repaints the metrics + series.

import { useState, useTransition } from "react";
import { reconstructAction, type ActionResult } from "@/app/portfolios/[id]/performance/actions";

export function BuildHistory({ portfolioId, feedOn }: { portfolioId: string; feedOn?: boolean }) {
  const [pending, start] = useTransition();
  const [result, setResult] = useState<ActionResult | null>(null);

  // Feed-gated (metron-ops#52): the beta backfills NAV forward from broker valuations,
  // not from spine history — hide the control when the feed is off.
  if (feedOn === false) return null;

  return (
    <div className="flex items-center gap-3">
      <button
        type="button"
        disabled={pending}
        onClick={() => start(async () => setResult(await reconstructAction(portfolioId)))}
        className="shrink-0 rounded border border-line px-3 py-1.5 text-sm font-medium hover:bg-white/5 disabled:opacity-50"
      >
        {pending ? "Building…" : "Build history from past prices"}
      </button>
      {result ? <span className={`text-sm ${result.ok ? "text-muted" : "text-negative"}`}>{result.message}</span> : null}
    </div>
  );
}
