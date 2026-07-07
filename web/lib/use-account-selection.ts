"use client";

// Shared account-selection driver (metron-ops-I156) — the optimistic `?account_id=`
// URL push originally built inside AccountPanel (metron-ops#77/#64), lifted so the
// Holdings toolbar's accounts scope chip and the AccountPanel run the SAME machinery:
// optimistic checkbox state (URL commits after the server round-trip), saved-selection
// persistence (fire-and-forget), and a transition flag for the in-flight re-fetch.

import { useCallback, useEffect, useMemo, useState, useTransition } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { saveAccountSelectionAction } from "@/app/portfolios/[id]/actions";

export function useAccountSelection(portfolioId: string | undefined, allIds: string[]) {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const [navPending, startNav] = useTransition();

  // The selection, as a stable comma-key, so the memo + callbacks below don't rebuild
  // a new Set every render. Empty URL selection = whole portfolio → every box checked.
  const urlKey = params.getAll("account_id").join(",");
  const selectedFromUrl = useMemo(() => new Set(urlKey ? urlKey.split(",") : allIds), [urlKey, allIds]);

  // OPTIMISTIC selection: flip instantly via a local pending set, reconcile to the URL
  // once the navigation lands (urlKey changes → clear the override).
  const [pendingSel, setPendingSel] = useState<Set<string> | null>(null);
  useEffect(() => {
    setPendingSel(null);
  }, [urlKey]);
  const selected = pendingSel ?? selectedFromUrl;

  const push = useCallback(
    async (ids: string[]) => {
      setPendingSel(new Set(ids.length === 0 ? allIds : ids));
      const qs = new URLSearchParams();
      // Preserve any other query params (?val=, ?combine=); replace the account_id set.
      params.forEach((value, key) => {
        if (key !== "account_id") qs.append(key, value);
      });
      ids.forEach((id) => qs.append("account_id", id));
      const s = qs.toString();
      if (portfolioId) {
        if (ids.length === 0) {
          // Clearing to "All" empties the URL — the page then applies the SAVED
          // selection, so the save must land first or it redirects back into the
          // stale filter. (Errors swallowed: filtering still works URL-driven.)
          await saveAccountSelectionAction(portfolioId, ids).catch(() => undefined);
        } else {
          void saveAccountSelectionAction(portfolioId, ids);
        }
      }
      startNav(() => {
        router.replace(s ? `${pathname}?${s}` : pathname, { scroll: false });
      });
    },
    [params, pathname, router, portfolioId, allIds],
  );

  const toggle = useCallback(
    (id: string) => {
      const next = new Set(selected);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      // Normalize "all" or "none" back to the whole-portfolio view (empty URL) so the
      // page never goes blank and the All toggle stays in sync.
      const ids = next.size === 0 || next.size === allIds.length ? [] : [...next];
      void push(ids);
    },
    [selected, allIds.length, push],
  );

  return { selected, viewingAll: selected.size === allIds.length, navPending, push, toggle };
}
