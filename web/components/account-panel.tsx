"use client";

// Accounts panel — the top-of-page selector. Lists every account with its own cost
// basis / unrealized / market value + institution + nickname + 3-way tax type, and a
// checkbox per account. The checked set drives a repeatable `?account_id=` query in the
// URL; the (server-rendered) tables + Risk/Attribution below read that selection and
// re-scope. Empty selection = whole portfolio (never a blank page).
//
// The selection also persists server-side (InvestorPreferences): every change is saved
// fire-and-forget, and pages landing with no ?account_id= apply the saved selection —
// so the filter survives reloads without the user re-checking boxes.
//
// Accounts are grouped by tax status with per-group subtotals + a grand total
// (metron-ops#46); the 3-way tax treatment is editable inline (reuses the Settings
// override) so a mis-derived status can be corrected without leaving the page.

import { useCallback, useMemo, useState, useTransition } from "react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import {
  deleteAccountAction,
  saveAccountSelectionAction,
  updateAccountTagsAction,
} from "@/app/portfolios/[id]/actions";
import type { Account } from "@/lib/api";
import { moneyWhole, percent, signClass, signedMoneyWhole } from "@/lib/format";

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

// Inline tax-treatment override (mirrors the Settings dropdown). "" = Auto (derive).
const TAX_TREATMENTS: { value: string; label: string }[] = [
  { value: "", label: "Auto" },
  { value: "taxable", label: "Taxable" },
  { value: "tax_deferred", label: "Tax-deferred" },
  { value: "tax_exempt", label: "Tax-exempt" },
];

// Display order for the tax-status groups (anything else sorts after, alphabetically).
const TAX_GROUP_ORDER = ["Taxable", "Tax-deferred", "Tax-exempt", "Tax-advantaged"];

type Subtotal = { cost: number | null; mv: number | null; unreal: number | null };

function subtotal(accts: Account[]): Subtotal {
  let cost = 0;
  let mv = 0;
  let unreal = 0;
  let haveCost = false;
  let haveMv = false;
  let haveUnreal = false;
  for (const a of accts) {
    if (a.cost_basis_base != null) {
      cost += a.cost_basis_base;
      haveCost = true;
    }
    if (a.market_value != null) {
      mv += a.market_value;
      haveMv = true;
    }
    if (a.unrealized_gain != null) {
      unreal += a.unrealized_gain;
      haveUnreal = true;
    }
  }
  return { cost: haveCost ? cost : null, mv: haveMv ? mv : null, unreal: haveUnreal ? unreal : null };
}

/** The 3-column money readout, shared by account rows and subtotal/total rows. */
function MetricCells({
  cost,
  unreal,
  mv,
  baseCurrency,
  muted,
}: {
  cost: number | null;
  unreal: number | null;
  mv: number | null;
  baseCurrency: string;
  muted?: boolean;
}) {
  const pct = unreal != null && cost ? unreal / cost : null;
  // FIXED-width columns so the Cost / Unrealized / Market figures line up vertically
  // across every account row, subtotal and the grand total (metron-ops#54 — auto-width
  // grid columns sized per-row, so larger-magnitude accounts shifted out of alignment).
  return (
    <div className="flex shrink-0 gap-x-6 text-right text-sm tabular-nums">
      <div className="w-24">
        <div className="text-[10px] uppercase tracking-wide text-muted">Cost</div>
        <div className={muted ? "text-muted" : undefined}>{cost != null ? moneyWhole(cost, baseCurrency) : "—"}</div>
      </div>
      <div className="w-32">
        <div className="text-[10px] uppercase tracking-wide text-muted">Unrealized</div>
        <div className={unreal != null ? signClass(unreal) : "text-muted"}>
          {unreal != null ? (
            <>
              {signedMoneyWhole(unreal, baseCurrency)}
              {pct != null ? <span className="ml-1 text-xs">({percent(pct)})</span> : null}
            </>
          ) : (
            "—"
          )}
        </div>
      </div>
      <div className="w-24">
        <div className="text-[10px] uppercase tracking-wide text-muted">Market</div>
        <div className={muted ? "text-muted" : undefined}>{mv != null ? moneyWhole(mv, baseCurrency) : "—"}</div>
      </div>
    </div>
  );
}

export function AccountPanel({
  accounts,
  baseCurrency,
  portfolioId,
  readOnly = false,
}: {
  accounts: Account[];
  baseCurrency: string;
  portfolioId: string;
  /** Read-only summary (the Overview): grouped metrics + subtotals, no checkboxes /
   *  delete / treatment-edit. Activation lives on the Holdings page (metron-ops#64). */
  readOnly?: boolean;
}) {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const [deleting, startDelete] = useTransition();
  const [saving, startSave] = useTransition();
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const allIds = useMemo(() => accounts.map((a) => a.account_id), [accounts]);
  // The selection, as a stable comma-key, so the memo + callbacks below don't rebuild
  // a new Set every render (and trip the exhaustive-deps lint).
  const urlKey = params.getAll("account_id").join(",");
  const viewingAll = urlKey === "";
  // Empty URL selection = viewing the whole portfolio → every box reads as checked.
  const selected = useMemo(() => new Set(urlKey ? urlKey.split(",") : allIds), [urlKey, allIds]);

  // Group accounts by tax status (preserving the incoming order within each group), in a
  // stable display order. One group → no subtotals (they'd duplicate the grand total).
  const groups = useMemo(() => {
    const map = new Map<string, Account[]>();
    for (const a of accounts) {
      const key = typeLabel(a);
      const bucket = map.get(key);
      if (bucket) bucket.push(a);
      else map.set(key, [a]);
    }
    return [...map.entries()].sort((x, y) => {
      const ix = TAX_GROUP_ORDER.indexOf(x[0]);
      const iy = TAX_GROUP_ORDER.indexOf(y[0]);
      return (ix === -1 ? 99 : ix) - (iy === -1 ? 99 : iy) || x[0].localeCompare(y[0]);
    });
  }, [accounts]);
  const grand = useMemo(() => subtotal(accounts), [accounts]);

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

  // Toggle a whole tax-status group at once (metron-ops#64): if every account in the
  // group is already selected, drop them all; otherwise add them all.
  const toggleGroup = useCallback(
    (accts: Account[]) => {
      const groupIds = accts.map((a) => a.account_id);
      const allSelected = groupIds.every((id) => selected.has(id));
      const next = new Set(selected);
      groupIds.forEach((id) => (allSelected ? next.delete(id) : next.add(id)));
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

  const setTreatment = useCallback(
    (a: Account, value: string) => {
      startSave(async () => {
        // The Settings PATCH revalidates the portfolio path; refresh re-renders with the
        // new derived taxable status (and re-groups the row). Fire-and-forget on failure.
        await updateAccountTagsAction(portfolioId, a.account_id, { tax_treatment: value || null }).catch(
          () => undefined,
        );
        router.refresh();
      });
    },
    [portfolioId, router],
  );

  if (accounts.length === 0) {
    return (
      <div className="rounded-lg border border-dashed border-line p-6 text-sm text-muted">No connected accounts.</div>
    );
  }

  const showGroups = groups.length > 1;

  function row(a: Account) {
    const cost = a.cost_basis_base;
    const mv = a.market_value;
    const unreal = a.unrealized_gain;
    return (
      <li key={a.account_id} className="flex items-center gap-3 border-b border-line px-4 py-3 last:border-0">
        {readOnly ? (
          <span className="h-4 w-4 shrink-0" aria-hidden="true" />
        ) : (
          <input
            type="checkbox"
            checked={selected.has(a.account_id)}
            onChange={() => toggle(a.account_id)}
            aria-label={`Include ${accountLabel(a)}`}
            className="h-4 w-4 shrink-0 rounded border-line"
          />
        )}
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-x-2">
            <Link
              href={`/portfolios/${portfolioId}/accounts/${a.account_id}`}
              className="font-medium hover:underline"
            >
              {accountLabel(a)}
            </Link>
            {a.institution ? <span className="text-xs text-muted">{a.institution}</span> : null}
            <span className="text-xs text-muted">{a.currency}</span>
            {readOnly ? (
              <span className="text-[10px] uppercase tracking-wide text-muted">{typeLabel(a)}</span>
            ) : (
              <select
                value={a.tax_treatment ?? ""}
                onChange={(e) => setTreatment(a, e.target.value)}
                disabled={saving}
                aria-label={`Tax treatment for ${accountLabel(a)}`}
                title="Tax status — change if it's wrong"
                className="rounded border border-line bg-surface px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-muted hover:text-ink disabled:opacity-50"
              >
                {TAX_TREATMENTS.map((t) => (
                  <option key={t.value} value={t.value}>
                    {t.value === "" ? typeLabel(a) : t.label}
                  </option>
                ))}
              </select>
            )}
            {a.n_unconverted > 0 ? (
              <span className="text-[10px] text-muted" title="Some holdings excluded — no FX rate cached">
                {a.n_unconverted} unconverted
              </span>
            ) : null}
          </div>
        </div>
        <MetricCells cost={cost} unreal={unreal} mv={mv} baseCurrency={baseCurrency} />
        {readOnly ? (
          <span className="w-6 shrink-0" aria-hidden="true" />
        ) : (
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
        )}
      </li>
    );
  }

  return (
    <div className="overflow-hidden rounded-lg border border-line">
      {readOnly ? null : (
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
      )}
      {deleteError ? (
        <div className="border-b border-line bg-rose-500/10 px-4 py-2 text-xs text-rose-300">{deleteError}</div>
      ) : null}
      {groups.map(([label, accts]) => {
        const sub = subtotal(accts);
        const groupAllSelected = accts.every((a) => selected.has(a.account_id));
        return (
          <div key={label}>
            {showGroups ? (
              <div className="flex items-center gap-3 border-b border-line bg-surface/60 px-4 py-1.5">
                {readOnly ? (
                  // Keep the 16px checkbox slot so the group label lines up with the rows
                  // below it (the interactive panel has a checkbox here) — metron-ops#54.
                  <span className="h-4 w-4 shrink-0" aria-hidden="true" />
                ) : (
                  <input
                    type="checkbox"
                    checked={groupAllSelected}
                    onChange={() => toggleGroup(accts)}
                    aria-label={`Toggle all ${label} accounts`}
                    title={`Include / exclude all ${label} accounts`}
                    className="h-4 w-4 shrink-0 rounded border-line"
                  />
                )}
                <span className="text-[11px] font-medium uppercase tracking-wide text-muted">
                  {label} · {accts.length}
                </span>
              </div>
            ) : null}
            <ul>{accts.map(row)}</ul>
            {showGroups ? (
              <div className="flex items-center gap-3 border-b border-line bg-surface/40 px-4 py-2">
                <div className="min-w-0 flex-1 pl-7 text-[11px] uppercase tracking-wide text-muted">
                  {label} subtotal
                </div>
                <MetricCells cost={sub.cost} unreal={sub.unreal} mv={sub.mv} baseCurrency={baseCurrency} muted />
                <span className="w-6 shrink-0" aria-hidden="true" />
              </div>
            ) : null}
          </div>
        );
      })}
      <div className="flex items-center gap-3 border-t border-line bg-surface px-4 py-2 font-medium">
        <div className="min-w-0 flex-1 pl-7 text-[11px] uppercase tracking-wide text-muted">All accounts total</div>
        <MetricCells cost={grand.cost} unreal={grand.unreal} mv={grand.mv} baseCurrency={baseCurrency} />
        <span className="w-6 shrink-0" aria-hidden="true" />
      </div>
    </div>
  );
}
