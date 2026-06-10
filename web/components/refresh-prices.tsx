"use client";

// "Refresh prices" button: posts to the price-refresh Server Action (tenant header
// stays server-side), then revalidation repaints holdings/summary with market value.

import { useState, useTransition } from "react";
import { refreshPricesAction, type ActionResult } from "@/app/portfolios/[id]/actions";

export function RefreshPrices({ portfolioId }: { portfolioId: string }) {
  const [pending, start] = useTransition();
  const [result, setResult] = useState<ActionResult | null>(null);

  return (
    <div className="flex items-center gap-3">
      <button
        type="button"
        disabled={pending}
        onClick={() => start(async () => setResult(await refreshPricesAction(portfolioId)))}
        className="shrink-0 rounded border border-line px-3 py-1.5 text-sm font-medium hover:bg-slate-50 disabled:opacity-50"
      >
        {pending ? "Refreshing…" : "Refresh prices"}
      </button>
      {result ? <span className={`text-sm ${result.ok ? "text-muted" : "text-negative"}`}>{result.message}</span> : null}
    </div>
  );
}
