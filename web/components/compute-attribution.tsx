"use client";

// "Compute attribution" button: resolves sectors + backfills history + runs the
// Brinson decomposition via the Server Action, then revalidation repaints it.

import { useState, useTransition } from "react";
import { useSearchParams } from "next/navigation";
import { computeAttributionAction, type ActionResult } from "@/app/portfolios/[id]/attribution/actions";

export function ComputeAttribution({ portfolioId }: { portfolioId: string }) {
  const [pending, start] = useTransition();
  const [result, setResult] = useState<ActionResult | null>(null);
  // Scope the compute to the account selection carried in the URL (empty = whole portfolio).
  const accountIds = useSearchParams().getAll("account_id");

  return (
    <div className="flex items-center gap-3">
      <button
        type="button"
        disabled={pending}
        onClick={() => start(async () => setResult(await computeAttributionAction(portfolioId, accountIds)))}
        className="shrink-0 rounded border border-line px-3 py-1.5 text-sm font-medium hover:bg-white/5 disabled:opacity-50"
      >
        {pending ? "Computing…" : "Compute attribution"}
      </button>
      {result ? <span className={`text-sm ${result.ok ? "text-muted" : "text-negative"}`}>{result.message}</span> : null}
    </div>
  );
}
