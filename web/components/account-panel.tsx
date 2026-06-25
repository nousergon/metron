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
// (metron-ops#46). The tax-status label is read-only here — editing the 3-way treatment
// lives on the Settings page (the grouping already shows the category, so the inline
// dropdown was redundant on the Overview).

import { useCallback, useEffect, useMemo, useState, useTransition } from "react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { deleteAccountAction, saveAccountSelectionAction } from "@/app/portfolios/[id]/actions";
import type { Account } from "@/lib/api";
import { accountingMoneyWhole, accountingPercent, moneyWhole, signClass } from "@/lib/format";

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

// FIXED column widths, shared by the header and every value row so the figures line up
// vertically across account rows, subtotals and the grand total (metron-ops#54 —
// auto-width columns sized per-row shifted larger-magnitude accounts out of alignment).
const COL_COST = "w-24";
const COL_UNREAL = "w-28"; // Unrealized $
const COL_UNREAL_PCT = "w-20"; // Unrealized %
const COL_MARKET = "w-24";
const COL_PERIOD = "w-16"; // Day / YTD / LTM % (metron-ops#87)

/** A single column-header row for the metric columns — replaces the per-row labels with
 *  one header. Unrealized is split into $ and % columns (metron-ops#80); Day/YTD/LTM are
 *  per-account period returns (metron-ops#87). */
function MetricHeader() {
  return (
    <div className="flex shrink-0 gap-x-6 text-right text-[10px] uppercase tracking-wide text-muted">
      <div className={COL_COST}>Cost</div>
      <div className={COL_UNREAL}>Unrealized $</div>
      <div className={COL_UNREAL_PCT}>Unrealized %</div>
      <div className={COL_MARKET}>Market</div>
      <div className={COL_PERIOD}>Day</div>
      <div className={COL_PERIOD}>YTD</div>
      <div className={COL_PERIOD}>LTM</div>
    </div>
  );
}

/** A single period-return cell: signed % when a number, "—" when null (no data), blank when
 *  undefined (a subtotal/total row, where period returns don't aggregate). */
function PeriodCell({ pct }: { pct?: number | null }) {
  if (pct === undefined) return <div className={COL_PERIOD} />;
  return <div className={`${COL_PERIOD} ${pct != null ? signClass(pct) : "text-muted"}`}>{pct != null ? accountingPercent(pct) : "—"}</div>;
}

/** The money readout, shared by account rows and subtotal/total rows. Labels live once in
 *  <MetricHeader>. Unrealized $ and % are separate columns; gains/losses read from color
 *  + parentheses (no leading "+"/"−") — accounting style (metron-ops#80). Day/YTD/LTM are
 *  passed only on account rows (undefined on subtotals/total → blank). */
function MetricCells({
  cost,
  unreal,
  mv,
  baseCurrency,
  muted,
  dayPct,
  ytdPct,
  ltmPct,
}: {
  cost: number | null;
  unreal: number | null;
  mv: number | null;
  baseCurrency: string;
  muted?: boolean;
  dayPct?: number | null;
  ytdPct?: number | null;
  ltmPct?: number | null;
}) {
  const pct = unreal != null && cost ? unreal / cost : null;
  const unrealClass = unreal != null ? signClass(unreal) : "text-muted";
  return (
    <div className="flex shrink-0 gap-x-6 text-right text-sm tabular-nums">
      <div className={`${COL_COST} ${muted ? "text-muted" : ""}`}>
        {cost != null ? moneyWhole(cost, baseCurrency) : "—"}
      </div>
      <div className={`${COL_UNREAL} ${unrealClass}`}>
        {unreal != null ? accountingMoneyWhole(unreal, baseCurrency) : "—"}
      </div>
      <div className={`${COL_UNREAL_PCT} ${unrealClass}`}>{pct != null ? accountingPercent(pct) : "—"}</div>
      <div className={`${COL_MARKET} ${muted ? "text-muted" : ""}`}>
        {mv != null ? moneyWhole(mv, baseCurrency) : "—"}
      </div>
      <PeriodCell pct={dayPct} />
      <PeriodCell pct={ytdPct} />
      <PeriodCell pct={ltmPct} />
    </div>
  );
}

export function AccountPanel({
  accounts,
  baseCurrency,
  portfolioId,
  selectable = true,
  deletable = false,
}: {
  accounts: Account[];
  baseCurrency: string;
  portfolioId: string;
  /** Temporary scoping: checkboxes per account + per tax-group + an All toggle, driving the
   *  ?account_id= selection. The Holdings filter view (metron-ops#77). */
  selectable?: boolean;
  /** Account MANAGEMENT: the per-account delete button (the Overview, metron-ops#77).
   *  Orthogonal to selectable so each page opts into the right capability. Tax-treatment
   *  editing lives on the Settings page, not here. */
  deletable?: boolean;
}) {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const [deleting, startDelete] = useTransition();
  const [navPending, startNav] = useTransition();
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const allIds = useMemo(() => accounts.map((a) => a.account_id), [accounts]);
  // The selection, as a stable comma-key, so the memo + callbacks below don't rebuild
  // a new Set every render (and trip the exhaustive-deps lint).
  const urlKey = params.getAll("account_id").join(",");
  // Empty URL selection = viewing the whole portfolio → every box reads as checked.
  const selectedFromUrl = useMemo(() => new Set(urlKey ? urlKey.split(",") : allIds), [urlKey, allIds]);

  // OPTIMISTIC selection: the checkbox state is normally derived from the URL, which only
  // updates AFTER the server round-trip (~0.5–1s) commits — so a click felt unresponsive
  // ("did nothing, then snapped"). We flip the boxes instantly via a local pending set and
  // reconcile to the URL once the navigation lands (urlKey changes → clear it). The data
  // sections below still re-fetch, but `navPending` drives a subtle "Updating…" cue so the
  // delay reads as in-progress, not broken.
  const [pendingSel, setPendingSel] = useState<Set<string> | null>(null);
  useEffect(() => {
    // The URL caught up with (or diverged from) the optimistic guess — drop the override.
    setPendingSel(null);
  }, [urlKey]);
  const selected = pendingSel ?? selectedFromUrl;
  const viewingAll = selected.size === allIds.length;

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
  // Totals reflect the ACTIVE selection, so the panel's total matches the headline
  // "Total value" (scoped to the same selection). Viewing-all → selected = every account,
  // so the grand total is the whole-portfolio value. Per-group subtotals scope the same
  // way, so they always sum to the grand total.
  const selectedAccounts = useMemo(
    () => accounts.filter((a) => selected.has(a.account_id)),
    [accounts, selected],
  );
  const grand = useMemo(() => subtotal(selectedAccounts), [selectedAccounts]);

  const pushSelection = useCallback(
    async (ids: string[]) => {
      // Optimistic: flip the boxes NOW (an empty `ids` means "all", so show every box on).
      setPendingSel(new Set(ids.length === 0 ? allIds : ids));
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
      // Drive the soft navigation through a transition so `navPending` reflects the
      // in-flight re-fetch of the data sections below (the optimistic boxes already moved).
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
    // Unselected rows dim — the figures that feed the (selection-scoped) totals read bold.
    const included = selected.has(a.account_id);
    return (
      <li
        key={a.account_id}
        className={`flex items-center gap-3 border-b border-line px-4 py-3 last:border-0 ${included ? "" : "opacity-50"}`}
      >
        {selectable ? (
          <input
            type="checkbox"
            checked={included}
            onChange={() => toggle(a.account_id)}
            aria-label={`Include ${accountLabel(a)}`}
            className="h-4 w-4 shrink-0 rounded border-line"
          />
        ) : (
          <span className="h-4 w-4 shrink-0" aria-hidden="true" />
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
            {/* The per-row tax-treatment label is redundant once the rows are grouped under
                a tax-status heading; show it only when there's a single group (no heading). */}
            {!showGroups ? (
              <span className="text-[10px] uppercase tracking-wide text-muted">{typeLabel(a)}</span>
            ) : null}
            {a.n_unconverted > 0 ? (
              <span className="text-[10px] text-muted" title="Some holdings excluded — no FX rate cached">
                {a.n_unconverted} unconverted
              </span>
            ) : null}
          </div>
        </div>
        <MetricCells
          cost={cost}
          unreal={unreal}
          mv={mv}
          baseCurrency={baseCurrency}
          dayPct={a.day_pct}
          ytdPct={a.ytd_pct}
          ltmPct={a.ltm_pct}
        />
        {deletable ? (
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
        ) : (
          <span className="w-6 shrink-0" aria-hidden="true" />
        )}
      </li>
    );
  }

  return (
    <div className="overflow-hidden rounded-lg border border-line">
      {selectable ? (
        <div className="flex items-center justify-between border-b border-line bg-surface px-4 py-2">
          <span className="flex items-center gap-2 text-xs uppercase tracking-wide text-muted">
            Select accounts to filter the tables &amp; charts below
            {navPending ? <span className="text-[10px] normal-case tracking-normal text-muted/70">· updating…</span> : null}
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
      ) : null}
      {deleteError ? (
        <div className="border-b border-line bg-rose-500/10 px-4 py-2 text-xs text-rose-300">{deleteError}</div>
      ) : null}
      {/* One column header for the whole panel — the metric labels no longer repeat per
          row (metron-ops). The 16px lead + w-6 trail spacers mirror the row layout so the
          Cost / Unrealized / Market headers sit directly over their columns in both modes. */}
      <div className="flex items-center gap-3 border-b border-line bg-surface px-4 py-2">
        <span className="h-4 w-4 shrink-0" aria-hidden="true" />
        <div className="min-w-0 flex-1 text-[10px] uppercase tracking-wide text-muted">Account</div>
        <MetricHeader />
        <span className="w-6 shrink-0" aria-hidden="true" />
      </div>
      {groups.map(([label, accts]) => {
        // Subtotal scopes to the selection, like the grand total — so subtotals always
        // sum to the grand total no matter which accounts are selected.
        const sub = subtotal(accts.filter((a) => selected.has(a.account_id)));
        const groupAllSelected = accts.every((a) => selected.has(a.account_id));
        return (
          <div key={label}>
            {showGroups ? (
              <div className="flex items-center gap-3 border-b border-line bg-surface/60 px-4 py-1.5">
                {selectable ? (
                  <input
                    type="checkbox"
                    checked={groupAllSelected}
                    onChange={() => toggleGroup(accts)}
                    aria-label={`Toggle all ${label} accounts`}
                    title={`Include / exclude all ${label} accounts`}
                    className="h-4 w-4 shrink-0 rounded border-line"
                  />
                ) : (
                  // Keep the 16px checkbox slot so the group label lines up with the rows
                  // below it (the management panel has no checkbox here) — metron-ops#54.
                  <span className="h-4 w-4 shrink-0" aria-hidden="true" />
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
        <div className="min-w-0 flex-1 pl-7 text-[11px] uppercase tracking-wide text-muted">
          {viewingAll ? "All accounts total" : "Selected accounts total"}
        </div>
        <MetricCells cost={grand.cost} unreal={grand.unreal} mv={grand.mv} baseCurrency={baseCurrency} />
        <span className="w-6 shrink-0" aria-hidden="true" />
      </div>
    </div>
  );
}
