"use client";

// Inline portfolio rename: shows the name as a heading with a "Rename" affordance;
// click → edit in place → save via the Server Action (which revalidates so the new
// name paints everywhere).

import { useState, useTransition } from "react";
import { renamePortfolioAction } from "@/app/portfolios/[id]/actions";
import { ReadOnlyNotice } from "@/components/ui";
import { isReferencePortfolio } from "@/lib/demo";

export function RenamePortfolio({ portfolioId, name }: { portfolioId: string; name: string }) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(name);
  const [pending, start] = useTransition();
  const [error, setError] = useState<string | null>(null);

  function save() {
    setError(null);
    start(async () => {
      const r = await renamePortfolioAction(portfolioId, value);
      if (r.ok) setEditing(false);
      else setError(r.message);
    });
  }

  // The Showcase Portfolio (metron-ops#120) is a fixed-name read-only mirror — the
  // API 403s the rename route regardless of caller tenant (api/main.py::_demo_read_only).
  if (isReferencePortfolio(portfolioId)) {
    return (
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-2xl font-semibold tracking-tight">{name}</h1>
        <ReadOnlyNotice>Illustrative — read-only, can&apos;t be renamed.</ReadOnlyNotice>
      </div>
    );
  }

  if (!editing) {
    return (
      <div className="flex items-center gap-3">
        <h1 className="text-2xl font-semibold tracking-tight">{name}</h1>
        <button
          type="button"
          onClick={() => {
            setValue(name);
            setError(null);
            setEditing(true);
          }}
          className="text-sm text-muted underline hover:text-ink"
        >
          Rename
        </button>
      </div>
    );
  }

  return (
    <div className="flex flex-wrap items-center gap-2">
      <input
        autoFocus
        className="rounded border border-line px-2 py-1 text-lg font-semibold"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") save();
          if (e.key === "Escape") setEditing(false);
        }}
      />
      <button
        type="button"
        disabled={pending}
        onClick={save}
        className="rounded bg-ink px-3 py-1 text-sm font-medium text-paper hover:bg-white disabled:opacity-50"
      >
        {pending ? "Saving…" : "Save"}
      </button>
      <button type="button" onClick={() => setEditing(false)} className="text-sm text-muted hover:text-ink">
        Cancel
      </button>
      {error ? <span className="text-sm text-negative">{error}</span> : null}
    </div>
  );
}
