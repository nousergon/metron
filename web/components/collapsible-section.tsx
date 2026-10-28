"use client";

// Shared controlled <details>/<summary> disclosure (metron-ops#178) — extracted from the
// pattern already used by BalanceByAccountPanel (holdings-view.tsx) and HoldingsWhatIfPanel:
// onClick (not the native <details> toggle event) drives open/closed via React state, and the
// body is only rendered while open, rather than relying on <details>'s native hidden-content
// behavior. Adds a ▾/▸ glyph so a bare (unboxed) heading still reads as collapsible. Callers:
// Holdings' per-group sections (by asset class, by account, by sector) and, since 2026-10-28,
// the Balance by account panel, which moved under the asset-class sections with the same
// heading. HoldingsWhatIfPanel keeps its own bordered-box markup.
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
