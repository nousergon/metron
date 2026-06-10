"use client";

// "Compute risk" button: backfills history + fits the factor model via the Server
// Action, then revalidation repaints the decomposition.

import { useState, useTransition } from "react";
import { computeRiskAction, type ActionResult } from "@/app/portfolios/[id]/risk/actions";

export function ComputeRisk({ portfolioId }: { portfolioId: string }) {
  const [pending, start] = useTransition();
  const [result, setResult] = useState<ActionResult | null>(null);

  return (
    <div className="flex items-center gap-3">
      <button
        type="button"
        disabled={pending}
        onClick={() => start(async () => setResult(await computeRiskAction(portfolioId)))}
        className="shrink-0 rounded border border-line px-3 py-1.5 text-sm font-medium hover:bg-slate-50 disabled:opacity-50"
      >
        {pending ? "Computing…" : "Compute risk"}
      </button>
      {result ? <span className={`text-sm ${result.ok ? "text-muted" : "text-negative"}`}>{result.message}</span> : null}
    </div>
  );
}
