"use client";

// "Refresh prices" button: posts to the price-refresh Server Action (tenant header
// stays server-side), then revalidation repaints holdings/summary with market value.

import { useState, useTransition } from "react";
import { refreshPricesAction, type ActionResult } from "@/app/portfolios/[id]/actions";
import { ReadOnlyNotice } from "@/components/ui";
import { isReferencePortfolio } from "@/lib/demo";

export function RefreshPrices({ portfolioId, feedOn }: { portfolioId: string; feedOn?: boolean }) {
  const [pending, start] = useTransition();
  const [result, setResult] = useState<ActionResult | null>(null);

  // Beta tier (feed off) values holdings from broker data only — the spine/feed-backed
  // refresh is feed-gated server-side (metron-ops#52), so hide the control here too.
  if (feedOn === false) return null;

  // The Reference Rate showcase (metron-ops#120) syncs daily from the engine's published
  // artifact — a manual refresh would just 403 (api/main.py::_demo_read_only).
  if (isReferencePortfolio(portfolioId)) {
    return <ReadOnlyNotice>Illustrative — read-only. This showcase portfolio refreshes automatically.</ReadOnlyNotice>;
  }

  return (
    <div className="flex items-center gap-3">
      <button
        type="button"
        disabled={pending}
        onClick={() => start(async () => setResult(await refreshPricesAction(portfolioId)))}
        className="shrink-0 rounded border border-line px-3 py-1.5 text-sm font-medium hover:bg-white/5 disabled:opacity-50"
      >
        {pending ? "Refreshing…" : "Refresh prices"}
      </button>
      {result ? <span className={`text-sm ${result.ok ? "text-muted" : "text-negative"}`}>{result.message}</span> : null}
    </div>
  );
}
