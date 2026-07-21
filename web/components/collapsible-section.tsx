"use client";

// Shared controlled <details>/<summary> disclosure (metron-ops#178) — extracted from the
// pattern already used by BalanceByAccountPanel (holdings-view.tsx) and HoldingsWhatIfPanel:
// onClick (not the native <details> toggle event) drives open/closed via React state, and the
// body is only rendered while open, rather than relying on <details>'s native hidden-content
// behavior. Adds a ▾/▸ glyph so a bare (unboxed) heading still reads as collapsible — the two
// existing bordered-panel adopters don't need it (the box itself is the affordance) and are
// left on their own inline markup rather than migrated, to keep this change scoped to the new
// callers (Holdings' per-group sections: by asset class, by account, by sector).
import { useState, type ReactNode } from "react";

export function CollapsibleSection({
  summary,
  defaultOpen = true,
  children,
  className = "",
}: {
  summary: ReactNode;
  defaultOpen?: boolean;
  children: ReactNode;
  className?: string;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <details className={className} open={open}>
      <summary
        className="flex cursor-pointer list-none items-start gap-1.5"
        onClick={(e) => {
          e.preventDefault();
          setOpen((v) => !v);
        }}
      >
        <span className="mt-0.5 shrink-0 text-[10px] text-muted" aria-hidden>
          {open ? "▾" : "▸"}
        </span>
        <div className="min-w-0 flex-1">{summary}</div>
      </summary>
      {open ? children : null}
    </details>
  );
}
