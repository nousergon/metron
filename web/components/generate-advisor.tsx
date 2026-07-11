"use client";

// "Generate / Regenerate" button for the Intelligence page. Runs the Claude narrative via
// the Server Action; revalidation repaints the fresh commentary.

import { useState, useTransition } from "react";
import { generateAdvisorAction, type ActionResult } from "@/app/portfolios/[id]/intelligence/actions";

export function GenerateAdvisor({ portfolioId, label }: { portfolioId: string; label: string }) {
  const [pending, start] = useTransition();
  const [result, setResult] = useState<ActionResult | null>(null);

  return (
    <div className="flex items-center gap-3">
      <button
        type="button"
        disabled={pending}
        onClick={() => start(async () => setResult(await generateAdvisorAction(portfolioId)))}
        className="shrink-0 rounded border border-line px-3 py-1.5 text-sm font-medium hover:bg-white/5 disabled:opacity-50"
      >
        {pending ? "Generating…" : label}
      </button>
      {result && !result.ok ? <span className="text-sm text-negative">{result.message}</span> : null}
    </div>
  );
}
