"use client";

// Shared Holdings type-filter state (the HIDDEN security_types) — lifted out of
// HoldingsView so the landing page's Portfolio total, hoisted to the TOP of the page (Brian,
// 2026-09-28), sums exactly the rows the table below shows. The total and the table are
// separate async Server Components under independent <Suspense> boundaries, so — as with
// ColumnBandsProvider (column-bands-context.tsx) — a thin client context mounted above both
// is the bridge.
//
// The provider is seeded server-side from the saved view's hidden types, so the first
// server render of the hoisted total already matches the table (no flash / layout shift).
// HoldingsView still owns persisting a change. A component rendered without the provider
// (e.g. an isolated unit test) gets independent local state via the fallback below.

import { createContext, useContext, useMemo, useState, type ReactNode } from "react";
import { PortfolioTotalBar } from "@/components/portfolio-total-bar";
import type { Holding } from "@/lib/api";

type HiddenTypesContextValue = {
  hidden: Set<string>;
  setHidden: (next: Set<string>) => void;
};

const HiddenTypesContext = createContext<HiddenTypesContextValue | null>(null);

export function HiddenTypesProvider({
  children,
  initialHidden,
}: {
  children: ReactNode;
  /** The saved view's hidden security types (metron-ops#115); null/omitted = all shown. */
  initialHidden?: string[] | null;
}) {
  const [hidden, setHidden] = useState<Set<string>>(() => new Set(initialHidden ?? []));
  const value = useMemo(() => ({ hidden, setHidden }), [hidden]);
  return <HiddenTypesContext.Provider value={value}>{children}</HiddenTypesContext.Provider>;
}

/** The shared hidden-types filter; falls back to local state (seeded from `fallbackInitial`)
 *  when rendered without a provider ancestor. */
export function useHiddenTypes(fallbackInitial?: string[] | null): HiddenTypesContextValue {
  const ctx = useContext(HiddenTypesContext);
  // Always called (rules-of-hooks) — only used when ctx is null.
  const [fallback, setFallback] = useState<Set<string>>(() => new Set(fallbackInitial ?? []));
  if (ctx) return ctx;
  return { hidden: fallback, setHidden: setFallback };
}

/** The landing page's hoisted Portfolio total — the same PortfolioTotalBar the groupers
 *  render, over the same type-filtered rows as the table. */
export function FilteredPortfolioTotal({
  holdings,
  baseCurrency,
  priced,
}: {
  holdings: Holding[];
  baseCurrency: string;
  priced: boolean;
}) {
  const { hidden } = useHiddenTypes();
  const shown = useMemo(
    () => (hidden.size ? holdings.filter((h) => !hidden.has(h.security_type)) : holdings),
    [holdings, hidden],
  );
  return <PortfolioTotalBar holdings={shown} baseCurrency={baseCurrency} priced={priced} />;
}
