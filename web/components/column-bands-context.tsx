"use client";

// Shared column-band selection — lifts the Holdings COLUMNS control's
// selection so the Watchlist comparison table (mounted as a separate async Server Component
// under its own <Suspense> boundary in app/portfolios/[id]/page.tsx) reflects the SAME bands
// the user picked on Holdings, instead of rendering its own hardcoded set. Fixes the bug
// where the two surfaces read as "one selector" but were structurally never wired together.
//
// Mirrors the LiveValuationProvider pattern (live-valuation-context.tsx): a thin client
// context carrying ONE piece of state down past the Suspense/Server-Component boundary,
// mounted in page.tsx wrapping BOTH the Holdings and Watchlist <Suspense> sections (the
// Suspense boundaries themselves stay separate — they exist for independent streaming, not
// removed here). Session-only, like the control it mirrors (holdings-view.tsx header
// comment): a fresh page load remounts the provider and resets to its regime-appropriate
// default (Intraday while live, Overview/DEFAULT_VISIBLE_GROUPS for settled — page.tsx).
//
// Unlike LiveValuationProvider this context is READ-WRITE — Holdings both reads and sets the
// selection (it owns the COLUMNS control); Watchlist only reads it. A component rendered
// without the provider (e.g. a standalone unit test) still gets a working, independently
// stateful default via useState in the fallback below, so callers aren't forced to wrap in
// the provider just to render.

import { createContext, useContext, useMemo, useState, type ReactNode } from "react";
import { DEFAULT_VISIBLE_GROUPS } from "@/components/holdings-column-presets";
import type { ColumnBand } from "@/components/holdings-table";

type ColumnBandsContextValue = {
  bands: ColumnBand[];
  setBands: (b: ColumnBand[]) => void;
};

const ColumnBandsContext = createContext<ColumnBandsContextValue | null>(null);

export function ColumnBandsProvider({
  children,
  initialBands = DEFAULT_VISIBLE_GROUPS,
}: {
  children: ReactNode;
  /** The initial column-preset selection. page.tsx passes the regime-appropriate default
   *  (Intraday while the live valuation regime is resolved, Overview for settled/no-live —
   *  see holdings-column-presets.tsx header comment); the DEFAULT_VISIBLE_GROUPS fallback
   *  here only fires for callers that don't pass one (e.g. isolated unit tests). */
  initialBands?: ColumnBand[];
}) {
  const [bands, setBands] = useState<ColumnBand[]>(initialBands);
  const value = useMemo(() => ({ bands, setBands }), [bands]);
  return <ColumnBandsContext.Provider value={value}>{children}</ColumnBandsContext.Provider>;
}

/** The Holdings COLUMNS selection, shared with any surface mounted under
 *  ColumnBandsProvider (currently Watchlist). Falls back to independent local state
 *  (still fully reactive) when rendered without a provider ancestor, so unit tests that
 *  mount a single component in isolation keep working. */
export function useColumnBands(): ColumnBandsContextValue {
  const ctx = useContext(ColumnBandsContext);
  // Always called (rules-of-hooks) — only used when ctx is null (no provider ancestor).
  const [fallbackBands, setFallbackBands] = useState<ColumnBand[]>(DEFAULT_VISIBLE_GROUPS);
  if (ctx) return ctx;
  return { bands: fallbackBands, setBands: setFallbackBands };
}
