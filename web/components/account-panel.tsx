"use client";

// Accounts panel — the top-of-page selector. Lists every account with its own cost
// basis / unrealized / market value + institution + nickname + 3-way tax type, and a
// checkbox per account. The checked set drives a repeatable `?account_id=` query in the
// URL; the (server-rendered) tables + Risk/Attribution below read that selection and
// re-scope. Empty selection = whole portfolio (never a blank page).
//
// The selection is URL-driven and session-scoped: pages default to the WHOLE portfolio
// (every box checked) and the URL filters within the session (metron-ops#113). Changes are
// still saved to InvestorPreferences fire-and-forget, but that value is no longer restored
// on landing — it's reserved for the Phase 2 saved-view work (metron-ops#114).
//
// Accounts are grouped by tax status with per-group subtotals + a grand total
// (metron-ops#46). The tax-status label is read-only on the Overview's selector usage —
// editing the 3-way treatment there still lives on the Settings page (the grouping already
// shows the category, so an inline dropdown was redundant). The Holdings "Balance by
// account" usage opts in via `editableClassification` (Brian, 2026-07-20): reclassifying
// mid-reconciliation without a trip to Settings is worth the small pencil affordance there.

import { useCallback, useEffect, useMemo, useState, useTransition } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { deleteAccountAction, updateAccountTagsAction } from "@/app/portfolios/[id]/actions";
import { useAccountSelection } from "@/lib/use-account-selection";
import type { Account } from "@/lib/api";
import { accountingMoneyWhole, accountingPercent, moneyWhole, signClass } from "@/lib/format";
import { isReferencePortfolio } from "@/lib/demo";
import { ReadOnlyNotice } from "@/components/ui";
import { TAX_TREATMENTS } from "@/components/settings-forms";

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

/** An account's total balance = holdings market value + cash — the brokerage-statement
 *  figure this panel exists to reconcile against (see the "Balance by account" wrapper
 *  in holdings-view.tsx). The two are separate API fields (analytics.AccountInfo keeps
 *  `market_value` as pure holdings valuation so `unrealized_gain` math elsewhere isn't
 *  affected) — this is where they're recombined for display. Null only when BOTH sides
 *  are unknown (no priced holdings AND no cash figure yet); a known side is never hidden
 *  just because the other is still unknown (a cash-only or holdings-only account still
 *  shows its known total, treating the missing side as 0). Cash was previously dropped
 *  entirely before reaching the API (metron-ops) — live case: $20.3k missing from the
 *  Crucible reference-rate sleeve. */
function accountBalance(a: Pick<Account, "market_value" | "cash">): number | null {
  if (a.market_value == null && a.cash == null) return null;
  return (a.market_value ?? 0) + (a.cash ?? 0);
}

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
    const bal = accountBalance(a);
    if (bal != null) {
      mv += bal;
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
function MetricHeader({ showDay = true }: { showDay?: boolean }) {
  return (
    <div className="flex shrink-0 gap-x-6 text-right text-[10px] uppercase tracking-wide text-muted">
      {/* Holdings market value + cash (metron-ops) — was "Market" (holdings only,
          silently dropping the account's cash balance from this reconciliation panel).
          Balance leads the column order (Brian, 2026-07-20) since it's the headline
          figure this panel exists to reconcile against a brokerage statement. */}
      <div className={COL_MARKET}>Balance</div>
      <div className={COL_UNREAL}>Unrealized $</div>
      <div className={COL_UNREAL_PCT}>Unrealized %</div>
      <div className={COL_COST}>Cost</div>
      {showDay ? <div className={COL_PERIOD}>Day</div> : null}
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
  showDay = true,
}: {
  cost: number | null;
  unreal: number | null;
  mv: number | null;
  baseCurrency: string;
  muted?: boolean;
  dayPct?: number | null;
  ytdPct?: number | null;
  ltmPct?: number | null;
  /** Session Day % is live-quote-derived — hidden entirely in the settled valuation
   *  regime (metron-ops#153) rather than rendered as a column of dashes. */
  showDay?: boolean;
}) {
  const pct = unreal != null && cost ? unreal / cost : null;
  const unrealClass = unreal != null ? signClass(unreal) : "text-muted";
  return (
    <div className="flex shrink-0 gap-x-6 text-right text-sm tabular-nums">
      <div className={`${COL_MARKET} ${muted ? "text-muted" : ""}`}>
        {mv != null ? moneyWhole(mv, baseCurrency) : "—"}
      </div>
      <div className={`${COL_UNREAL} ${unrealClass}`}>
        {unreal != null ? accountingMoneyWhole(unreal, baseCurrency) : "—"}
      </div>
      <div className={`${COL_UNREAL_PCT} ${unrealClass}`}>{pct != null ? accountingPercent(pct) : "—"}</div>
      <div className={`${COL_COST} ${muted ? "text-muted" : ""}`}>
        {cost != null ? moneyWhole(cost, baseCurrency) : "—"}
      </div>
      {showDay ? <PeriodCell pct={dayPct} /> : null}
      <PeriodCell pct={ytdPct} />
      <PeriodCell pct={ltmPct} />
    </div>
  );
}

/** Pencil-triggered inline editor for an account's 3-way tax classification (Holdings
 *  "Balance by account" panel only, see `editableClassification` on <AccountPanel>). Saves
 *  through the same `updateAccountTagsAction` the Settings page uses, so the two surfaces
 *  can never define "taxable"/"tax_deferred"/"tax_exempt" differently. */
function ClassificationEditor({ account, portfolioId }: { account: Account; portfolioId: string }) {
  const router = useRouter();
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(account.tax_treatment ?? "");
  const [pending, startSave] = useTransition();
  const [error, setError] = useState<string | null>(null);

  function save() {
    setError(null);
    startSave(async () => {
      const r = await updateAccountTagsAction(portfolioId, account.account_id, { tax_treatment: value || null });
      if (!r.ok) {
        setError(r.message);
        return;
      }
      setEditing(false);
      // The account's tax-status GROUP may have just changed — refresh so the row
      // re-sorts into its new group instead of showing a stale label under the old one.
      router.refresh();
    });
  }

  if (!editing) {
    return (
      <button
        type="button"
        onClick={() => setEditing(true)}
        aria-label={`Edit tax classification for ${accountLabel(account)}`}
        title="Edit tax classification"
        className="shrink-0 text-[10px] leading-none text-muted/50 transition hover:text-ink"
      >
        <span aria-hidden="true">✎</span>
      </button>
    );
  }

  return (
    <span className="flex shrink-0 items-center gap-1.5">
      <select
        className="rounded border border-line bg-transparent px-1 py-0.5 text-[10px] uppercase tracking-wide"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        disabled={pending}
        autoFocus
      >
        {TAX_TREATMENTS.map((t) => (
          <option key={t.value} value={t.value}>
            {t.label}
          </option>
        ))}
      </select>
      <button
        type="button"
        disabled={pending}
        onClick={save}
        className="text-[10px] font-medium text-accent underline disabled:opacity-50"
      >
        {pending ? "Saving…" : "Save"}
      </button>
      <button
        type="button"
        disabled={pending}
        onClick={() => {
          setEditing(false);
          setValue(account.tax_treatment ?? "");
          setError(null);
        }}
        className="text-[10px] text-muted underline disabled:opacity-50"
      >
        Cancel
      </button>
      {error ? <span className="text-[10px] text-negative">{error}</span> : null}
    </span>
  );
}

export function AccountPanel({
  accounts,
  baseCurrency,
  portfolioId,
  selectable = true,
  deletable = false,
  showDay = true,
  editableClassification = false,
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
  /** Render the per-account session Day % (live valuation mode only, metron-ops#153). */
  showDay?: boolean;
  /** Small pencil affordance per row to reclassify taxable/tax-deferred/tax-exempt inline
   *  (the Holdings "Balance by account" usage, Brian 2026-07-20) — see <ClassificationEditor>.
   *  Off by default: the Overview's selector usage keeps tax-status read-only here (Settings
   *  page owns it there) so the checkbox row doesn't get busier than it needs to be. */
  editableClassification?: boolean;
}) {
  // The Showcase Portfolio (metron-ops#120) is a live, real-tenant-visible read-only
  // mirror (metron#162) — the API 403s account delete for it regardless of caller tenant
  // (api/main.py::_demo_read_only). Only relevant in `deletable` mode (the Overview); the
  // Holdings filter view never renders a delete affordance in the first place.
  const readOnly = deletable && isReferencePortfolio(portfolioId);
  const router = useRouter();
  const [deleting, startDelete] = useTransition();
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const allIds = useMemo(() => accounts.map((a) => a.account_id), [accounts]);
  // Selection machinery lifted to the shared hook (metron-ops-I156) — the Holdings
  // toolbar's accounts scope chip drives the SAME optimistic `?account_id=` push, so
  // the two surfaces can't drift.
  const { selected, viewingAll, navPending, push: pushSelection, toggle } = useAccountSelection(
    portfolioId,
    allIds,
  );

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
    const mv = accountBalance(a);
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
            // De-emphasized: a small, muted box that only gains accent + full opacity
            // when checked — selection is the common case, so the control reads as quiet
            // background chrome rather than a loud call to action (metron-ops#118+).
            className={`h-3.5 w-3.5 shrink-0 rounded border-line accent-muted transition ${included ? "opacity-90 accent-accent" : "opacity-40 hover:opacity-70"}`}
          />
        ) : (
          <span className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
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
                a tax-status heading; show it only when there's a single group (no heading) —
                UNLESS classification is editable here, where the label + pencil need to stay
                visible on every row (reclassifying moves the row to a different group). */}
            {!showGroups || editableClassification ? (
              <span className="text-[10px] uppercase tracking-wide text-muted">{typeLabel(a)}</span>
            ) : null}
            {editableClassification ? <ClassificationEditor account={a} portfolioId={portfolioId} /> : null}
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
          showDay={showDay}
        />
        {deletable && !readOnly ? (
          <button
            type="button"
            onClick={() => remove(a)}
            disabled={deleting}
            aria-label={`Delete ${accountLabel(a)}`}
            title="Delete this account and its data (future syncs skip it; restore from Settings)"
            // A small, dark, understated × — deletion is rare (reclassifying securities
            // covers most needs), so it reads as quiet chrome until hovered (metron-ops#118+).
            className="flex h-6 w-6 shrink-0 items-center justify-center rounded text-base leading-none text-muted/50 transition hover:bg-rose-500/10 hover:text-rose-300 disabled:opacity-50"
          >
            <span aria-hidden="true">×</span>
          </button>
        ) : (
          <span className="w-6 shrink-0" aria-hidden="true" />
        )}
      </li>
    );
  }

  return (
    // The metric columns are fixed-width and, together, wider than a phone viewport. Float
    // the panel to a min-width past that column sum and let the bordered wrapper scroll it
    // horizontally (overflow-x-auto) — the same pattern the <Table> and holdings tables use.
    // Without this the account column (min-w-0 flex-1) collapses to zero on a narrow screen
    // and the figures overlap the labels with no way to reach the cut-off columns (mobile
    // fix). The floor tracks the column count: the live Day column adds one more. On desktop
    // the panel is already wider than the floor, so nothing changes there.
    <div className="overflow-x-auto rounded-lg border border-line">
      <div className={showDay ? "min-w-[62rem]" : "min-w-[56rem]"}>
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
              className={`h-3.5 w-3.5 rounded border-line accent-muted transition ${viewingAll ? "opacity-90 accent-accent" : "opacity-40 hover:opacity-70"}`}
            />
            All accounts
          </label>
        </div>
      ) : null}
      {deleteError ? (
        <div className="border-b border-line bg-rose-500/10 px-4 py-2 text-xs text-rose-300">{deleteError}</div>
      ) : null}
      {readOnly ? (
        <div className="border-b border-line bg-surface px-4 py-2">
          <ReadOnlyNotice>Illustrative — read-only. Accounts on this showcase portfolio can&apos;t be deleted.</ReadOnlyNotice>
        </div>
      ) : null}
      {/* One column header for the whole panel — the metric labels no longer repeat per
          row (metron-ops). The 16px lead + w-6 trail spacers mirror the row layout so the
          Cost / Unrealized / Market headers sit directly over their columns in both modes. */}
      <div className="flex items-center gap-3 border-b border-line bg-surface px-4 py-2">
        <span className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
        <div className="min-w-0 flex-1 text-[10px] uppercase tracking-wide text-muted">Account</div>
        <MetricHeader showDay={showDay} />
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
                    className={`h-3.5 w-3.5 shrink-0 rounded border-line accent-muted transition ${groupAllSelected ? "opacity-90 accent-accent" : "opacity-40 hover:opacity-70"}`}
                  />
                ) : (
                  // Keep the checkbox slot so the group label lines up with the rows
                  // below it (the management panel has no checkbox here) — metron-ops#54.
                  <span className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
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
                <MetricCells cost={sub.cost} unreal={sub.unreal} mv={sub.mv} baseCurrency={baseCurrency} muted showDay={showDay} />
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
        <MetricCells cost={grand.cost} unreal={grand.unreal} mv={grand.mv} baseCurrency={baseCurrency} showDay={showDay} />
        <span className="w-6 shrink-0" aria-hidden="true" />
      </div>
      </div>
    </div>
  );
}
