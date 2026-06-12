"use client";

// Accounts panel — the top-of-page selector. Lists every account with its own cost
// basis / unrealized / market value + institution + nickname + 3-way type, and a
// checkbox per account. The checked set drives a repeatable `?account_id=` query in the
// URL; the (server-rendered) tables + Risk/Attribution below read that selection and
// re-scope. Empty selection = whole portfolio (never a blank page).
//
// The selection also persists server-side (InvestorPreferences): every change is saved
// fire-and-forget, and pages landing with no ?account_id= apply the saved selection —
// so the filter survives reloads without the user re-checking boxes.

import { useCallback, useMemo, useState, useTransition } from "react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { deleteAccountAction, saveAccountSelectionAction } from "@/app/portfolios/[id]/actions";
import type { Account } from "@/lib/api";
import { money, percent, signClass, signedMoney } from "@/lib/format";

/** Human label for the 3-way tax treatment, falling back to the derived taxable flag. */
function typeLabel(a: Account): string {
  switch (a.tax_treatment) {
    case "taxable":
      return "Taxable";
    case "tax_deferred":
      return "Tax-deferred";
    case "tax_exempt":
      return "Tax-exempt";
    default:
      return a.taxable ? "Taxable" : "Tax-advantaged";
  }
}

function accountLabel(a: Account): string {
  return a.nickname || a.name || a.external_id;
}

export function AccountPanel({
  accounts,
  baseCurrency,
  portfolioId,
}: {
  accounts: Account[];
  baseCurrency: string;
  portfolioId: string;
}) {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const [deleting, startDelete] = useTransition();
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const allIds = useMemo(() => accounts.map((a) => a.account_id), [accounts]);
  // The selection, as a stable comma-key, so the memo + callbacks below don't rebuild
  // a new Set every render (and trip the exhaustive-deps lint).
  const urlKey = params.getAll("account_id").join(",");
  const viewingAll = urlKey === "";
  // Empty URL selection = viewing the whole portfolio → every box reads as checked.
  const selected = useMemo(() => new Set(urlKey ? urlKey.split(",") : allIds), [urlKey, allIds]);

  const pushSelection = useCallback(
    async (ids: string[]) => {
      const qs = new URLSearchParams();
      // Preserve any other query params; replace the account_id set.
      params.forEach((value, key) => {
        if (key !== "account_id") qs.append(key, value);
      });
      ids.forEach((id) => qs.append("account_id", id));
      const s = qs.toString();
      if (ids.length === 0) {
        // Clearing to "All" empties the URL — the page then applies the SAVED
        // selection, so the save must land first or it redirects back into the
        // stale filter. (Errors swallowed: filtering still works URL-driven.)
        await saveAccountSelectionAction(portfolioId, ids).catch(() => undefined);
      } else {
        // Persist server-side so the selection survives reloads. Fire-and-forget: a
        // save failure must never block the URL-driven filtering.
        void saveAccountSelectionAction(portfolioId, ids);
      }
      router.replace(s ? `${pathname}?${s}` : pathname, { scroll: false });
    },
    [params, pathname, router, portfolioId],
  );

  const toggle = useCallback(
    (id: string) => {
      const next = new Set(selected);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      // Normalize "all" or "none" back to the whole-portfolio view (empty URL) so the
      // page never goes blank and the All toggle stays in sync.
      const ids = next.size === 0 || next.size === allIds.length ? [] : [...next];
      pushSelection(ids);
    },
    [selected, allIds.length, pushSelection],
  );

  const remove = useCallback(
    (a: Account) => {
      const ok = window.confirm(
        `Delete "${accountLabel(a)}" and all its imported data?\n\n` +
          "Future syncs will skip this account; you can restore it from Settings and re-sync.",
      );
      if (!ok) return;
      setDeleteError(null);
      startDelete(async () => {
        const result = await deleteAccountAction(portfolioId, a.account_id);
        if (!result.ok) {
          setDeleteError(result.message);
          return;
        }
        // Drop the deleted id from the URL selection so the pages below don't 404
        // scoping to a gone account; refresh re-renders with the account removed.
        const ids = [...selected].filter((id) => id !== a.account_id && allIds.includes(id));
        pushSelection(ids.length === allIds.length - 1 ? [] : ids);
        router.refresh();
      });
    },
    [portfolioId, selected, allIds, pushSelection, router],
  );

  if (accounts.length === 0) {
    return (
      <div className="rounded-lg border border-dashed border-line p-6 text-sm text-muted">No connected accounts.</div>
    );
  }

  return (
    <div className="overflow-hidden rounded-lg border border-line">
      <div className="flex items-center justify-between border-b border-line bg-surface px-4 py-2">
        <span className="text-xs uppercase tracking-wide text-muted">
          Select accounts to filter the tables &amp; charts below
        </span>
        <label className="flex cursor-pointer items-center gap-2 text-xs text-muted">
          <input
            type="checkbox"
            checked={viewingAll}
            onChange={() => pushSelection([])}
            className="h-4 w-4 rounded border-line"
          />
          All accounts
        </label>
      </div>
      {deleteError ? (
        <div className="border-b border-line bg-rose-500/10 px-4 py-2 text-xs text-rose-300">{deleteError}</div>
      ) : null}
      <ul>
        {accounts.map((a) => {
          const cost = a.cost_basis_base;
          const mv = a.market_value;
          const unreal = a.unrealized_gain;
          const pct = unreal != null && cost ? unreal / cost : null;
          return (
            <li key={a.account_id} className="flex items-center gap-3 border-b border-line px-4 py-3 last:border-0">
              <input
                type="checkbox"
                checked={selected.has(a.account_id)}
                onChange={() => toggle(a.account_id)}
                aria-label={`Include ${accountLabel(a)}`}
                className="h-4 w-4 shrink-0 rounded border-line"
              />
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-baseline gap-x-2">
                  <Link href={`/portfolios/${portfolioId}/accounts/${a.account_id}`} className="font-medium hover:underline">
                    {accountLabel(a)}
                  </Link>
                  {a.institution ? <span className="text-xs text-muted">{a.institution}</span> : null}
                  <span className="text-xs text-muted">{a.currency}</span>
                  <span className="rounded bg-white/10 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-muted">
                    {typeLabel(a)}
                  </span>
                  {a.n_unconverted > 0 ? (
                    <span className="text-[10px] text-muted" title="Some holdings excluded — no FX rate cached">
                      {a.n_unconverted} unconverted
                    </span>
                  ) : null}
                </div>
              </div>
              <div className="grid shrink-0 grid-cols-3 gap-x-6 text-right text-sm tabular-nums">
                <div>
                  <div className="text-[10px] uppercase tracking-wide text-muted">Cost</div>
                  <div>{cost != null ? money(cost, baseCurrency) : "—"}</div>
                </div>
                <div>
                  <div className="text-[10px] uppercase tracking-wide text-muted">Unrealized</div>
                  <div className={unreal != null ? signClass(unreal) : "text-muted"}>
                    {unreal != null ? (
                      <>
                        {signedMoney(unreal, baseCurrency)}
                        {pct != null ? <span className="ml-1 text-xs">({percent(pct)})</span> : null}
                      </>
                    ) : (
                      "—"
                    )}
                  </div>
                </div>
                <div>
                  <div className="text-[10px] uppercase tracking-wide text-muted">Market</div>
                  <div>{mv != null ? money(mv, baseCurrency) : "—"}</div>
                </div>
              </div>
              <button
                type="button"
                onClick={() => remove(a)}
                disabled={deleting}
                aria-label={`Delete ${accountLabel(a)}`}
                title="Delete this account and its data (future syncs skip it; restore from Settings)"
                className="shrink-0 rounded p-1 text-muted hover:bg-rose-500/10 hover:text-rose-300 disabled:opacity-50"
              >
                <svg viewBox="0 0 20 20" fill="currentColor" className="h-4 w-4" aria-hidden="true">
                  <path
                    fillRule="evenodd"
                    d="M8.75 1A2.75 2.75 0 0 0 6 3.75v.443c-.795.077-1.584.176-2.365.298a.75.75 0 1 0 .23 1.482l.149-.022.841 10.518A2.75 2.75 0 0 0 7.596 19h4.807a2.75 2.75 0 0 0 2.742-2.53l.841-10.52.149.023a.75.75 0 0 0 .23-1.482 41.03 41.03 0 0 0-2.365-.298V3.75A2.75 2.75 0 0 0 11.25 1h-2.5ZM10 4c.84 0 1.673.025 2.5.075V3.75c0-.69-.56-1.25-1.25-1.25h-2.5c-.69 0-1.25.56-1.25 1.25v.325C8.327 4.025 9.16 4 10 4ZM8.58 7.72a.75.75 0 0 0-1.5.06l.3 7.5a.75.75 0 1 0 1.5-.06l-.3-7.5Zm4.34.06a.75.75 0 1 0-1.5-.06l-.3 7.5a.75.75 0 1 0 1.5.06l.3-7.5Z"
                    clipRule="evenodd"
                  />
                </svg>
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
