"use client";

// Live-valuation display context (metron-ops#145/#147). Carries ONE fact — "the intraday
// overlay is currently applied to the values on this page" — from the page that fetched
// `GET /{id}/intraday` down to presentational leaves (the Holdings table's provenance
// markers) without threading a prop through every grouper in between. Default FALSE:
// a surface that never mounts the provider (watchlist compare, cost-basis views) makes
// no live claims, which is the conservative, correct default.

import { createContext, useContext, type ReactNode } from "react";

const LiveValuationContext = createContext(false);

export function LiveValuationProvider({ live, children }: { live: boolean; children: ReactNode }) {
  return <LiveValuationContext.Provider value={live}>{children}</LiveValuationContext.Provider>;
}

/** True when the current page's values are being revalued from the delayed intraday overlay. */
export function useLiveValuation(): boolean {
  return useContext(LiveValuationContext);
}
