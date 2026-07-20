"use client";

// Holdings toolbar + grouping switch. Three controls (metron-ops#114): the COMBINE toggle —
// Combined (one row per ticker) vs By account (one row per account-position, +Account
// column) — the GROUPING — "By asset class" / "By sector → country" / "By account" — and the
// COLUMN PRESET (which metric bands show, over the always-on position spine). Combine is
// URL-driven (it changes the holdings DATA shape, fetched server-side); grouping + preset are
// client-side presentational state. Grouping / combine / type-filter / valuation persist to
// InvestorPreferences (metron-ops#114) so the view survives reloads. The COLUMN PRESET is
// deliberately session-only: Holdings is the landing page, and landing must always open on
// the lean Overview set — an analytic lens (Attractiveness, Valuation, …) left selected
// yesterday must not become today's landing view (Brian, 2026-07-08). Switching presets
// mid-session works as before; a fresh load resets to Overview.
//
// The COLUMN PRESET selection now lives in ColumnBandsProvider (column-bands-context.tsx),
// lifted out of local state so the Watchlist comparison table further down the page reads
// the SAME band selection instead of its own hardcoded set (metron-ops#121 sync fix). The
// session-only behavior above is unchanged — the provider is mounted fresh per page load
// and still initializes to DEFAULT_VISIBLE_GROUPS.

import { useMemo, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { GroupedByAccount } from "@/components/grouped-by-account";
import { GroupedByClassification } from "@/components/grouped-by-classification";
import { GroupedHoldings } from "@/components/grouped-holdings";
import { TypeFilterChips } from "@/components/holdings-type-filter";
import { AccountScopeChip } from "@/components/account-scope-chip";
import { AccountPanel } from "@/components/account-panel";
import { ColumnPresetControl } from "@/components/holdings-column-presets";
import { type ColumnBand } from "@/components/holdings-table";
import { useColumnBands } from "@/components/column-bands-context";
import { HoldingsWhatIfPanel } from "@/components/holdings-whatif-panel";
import { saveHoldingsViewAction } from "@/app/portfolios/[id]/actions";
import type { Account, Holding, ValuationMedians } from "@/lib/api";

// Balance-by-account tie-out panel (metron-ops, Brian 2026-07-20) — a collapsible
// section pinned to the TOP of Holdings, above the toolbar, so the per-account balances
// used to reconcile against brokerage statements are always the first thing on the page
// instead of buried in the AccountScopeChip popover (which only has room for a compact
// list and was getting visually cut off by the table's sticky header — see the z-index
// fix on AccountScopeChip's panel). Reuses AccountPanel's grouping/subtotal logic
// (same component the Overview's Accounts panel uses) so the numbers can never drift
// between the two surfaces; selectable=false because account SCOPING already lives in
// the toolbar's AccountScopeChip — this panel is for reconciliation, not filtering.
// editableClassification=true (Brian 2026-07-20): a per-row pencil lets you fix a
// mis-tagged taxable/tax-deferred/tax-exempt account right where the mismatch is spotted,
// without a trip to Settings. Defaults open (the point is to have it visible without an
// extra click); collapsible so it can be tucked away once tied out.
function BalanceByAccountPanel({
  accounts,
  baseCurrency,
  portfolioId,
  showDay,
}: {
  accounts: Account[];
  baseCurrency: string;
  portfolioId: string;
  showDay: boolean;
}) {
  const [open, setOpen] = useState(true);
  return (
    <details className="rounded-lg border border-line" open={open}>
      {/* onClick (not the native <details> toggle event) so open/closed is driven by
          React state, same pattern as HoldingsWhatIfPanel — see that file's comment. */}
      <summary
        className="cursor-pointer list-none rounded-lg px-4 py-3 text-sm font-medium text-ink"
        onClick={(e) => {
          e.preventDefault();
          setOpen((v) => !v);
        }}
      >
        Balance by account
      </summary>
      {open ? (
        <div className="border-t border-line p-3">
          <AccountPanel
            accounts={accounts}
            baseCurrency={baseCurrency}
            portfolioId={portfolioId}
            selectable={false}
            deletable={false}
            showDay={showDay}
            editableClassification
          />
        </div>
      ) : null}
    </details>
  );
}

type Mode = "account" | "asset" | "classification";

const ALL_MODES: Mode[] = ["account", "asset", "classification"];

// "By account" sections the per-account rows by account; only meaningful (and only offered)
// on the by-account data, so it's appended to the grouping control when Combine = By account.
const ACCOUNT_MODE: { key: Mode; label: string } = { key: "account", label: "By account" };
const BASE_MODES: { key: Mode; label: string }[] = [
  { key: "asset", label: "By asset class" },
  { key: "classification", label: "By sector → country" },
];

const SEG_BTN = (active: boolean) =>
  `rounded-md px-2.5 py-1 transition ${active ? "bg-surface font-medium text-ink" : "text-muted hover:text-ink"}`;

/** Saved grouping → a valid Mode (defaults to asset-class). */
function initialMode(saved: string | null): Mode {
  return ALL_MODES.includes(saved as Mode) ? (saved as Mode) : "asset";
}

/** Combined vs By-account, driven by the `?combine=` URL param (server re-fetches the
 *  matching holdings shape). Preserves the rest of the query (e.g. account_id scope). */
function CombineToggle({ byAccount, onChange }: { byAccount: boolean; onChange: (on: boolean) => void }) {
  return (
    <div className="inline-flex rounded-lg border border-line p-0.5 text-xs">
      <button type="button" onClick={() => onChange(false)} className={SEG_BTN(!byAccount)} aria-pressed={!byAccount}>
        Combined
      </button>
      <button type="button" onClick={() => onChange(true)} className={SEG_BTN(byAccount)} aria-pressed={byAccount}>
        By account
      </button>
    </div>
  );
}

/** The valuation-regime selector (metron-ops#153/#156): the session view vs Settled close
 *  (the official EOD valuation). URL-driven like Combine — it changes the holdings DATA
 *  (the server re-fetches with `?valuation=`), and the whole page follows the regime.
 *  MARKET-STATE-AWARE (metron-ops-I156): the first option is labeled "Live session" only
 *  while the market is open ("live"); post-close the same data is honestly "Today's
 *  session" ("recap"); pre-market/weekend/holiday ("closed") the option grays out — a
 *  prior session has nothing live mode can add over settled, and a control must never
 *  imply live-ness after hours. */
function ValuationToggle({
  live,
  sessionState,
  onChange,
}: {
  live: boolean;
  sessionState: "live" | "recap" | "closed";
  onChange: (live: boolean) => void;
}) {
  const closed = sessionState === "closed";
  const liveLabel = sessionState === "recap" ? "Today's session" : "Live session";
  const liveTitle = closed
    ? "Market closed — no session data beyond the settled close"
    : sessionState === "recap"
      ? "The completed session's decomposition — session closed, values as of the close"
      : "Value positions from delayed intraday quotes while the session is open, with covered-basis session detail";
  return (
    <div className="inline-flex rounded-lg border border-line p-0.5 text-xs">
      <button
        type="button"
        onClick={closed ? undefined : () => onChange(true)}
        disabled={closed}
        className={closed ? "cursor-not-allowed rounded-md px-2.5 py-1 text-muted/40" : SEG_BTN(live)}
        aria-pressed={live && !closed}
        aria-disabled={closed}
        title={liveTitle}
      >
        {liveLabel}
      </button>
      <button
        type="button"
        onClick={() => onChange(false)}
        className={SEG_BTN(!live || closed)}
        aria-pressed={!live || closed}
        title="Value positions from the official end-of-day close — the settled record"
      >
        Settled close
      </button>
    </div>
  );
}

export function HoldingsView({
  holdings,
  baseCurrency,
  priced,
  medians,
  portfolioId,
  byAccount = false,
  savedGrouping = null,
  savedHiddenTypes = null,
  valuation = "settled",
  liveAvailable = false,
  sessionState = "closed",
  accounts,
  selectedAccountIds,
}: {
  holdings: Holding[];
  baseCurrency: string;
  priced: boolean;
  medians: ValuationMedians | null;
  portfolioId?: string;
  /** Uncombined view — holdings carry per-account rows; render the Account column. */
  byAccount?: boolean;
  /** Saved view (metron-ops#114/#115) — hydrate grouping / hidden types. The column
   *  preset is NOT hydrated: landing always opens on Overview (see header comment). */
  savedGrouping?: string | null;
  savedHiddenTypes?: string[] | null;
  /** The active valuation regime (metron-ops#153) — resolved server-side (URL param →
   *  saved view → default). The toggle re-fetches via `?val=`. */
  valuation?: "live" | "settled";
  /** Whether the live regime is offered at all (feed entitled + intraday toggle on). */
  liveAvailable?: boolean;
  /** Market/session state (metron-ops-I156) — labels/gates the toggle's session option. */
  sessionState?: "live" | "recap" | "closed";
  /** Accounts for the toolbar scope chip (metron-ops-I156); omitted → no chip. */
  accounts?: Account[];
  /** The active `?account_id=` selection (empty = whole portfolio). */
  selectedAccountIds?: string[];
}) {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const [mode, setMode] = useState<Mode>(() => initialMode(savedGrouping));
  // Shared with Watchlist via ColumnBandsProvider (see header comment) — always lands on the
  // lean Overview set (the provider's own initial state), deliberately not hydrated from the
  // saved view.
  const { bands: visibleGroups, setBands: setVisibleGroups } = useColumnBands();
  // Faceted type filter (metron-ops#115) — the set of HIDDEN security_types (empty = all
  // shown), hydrated from + persisted to the saved view like the other controls.
  const [hiddenTypes, setHiddenTypes] = useState<Set<string>>(() => new Set(savedHiddenTypes ?? []));
  const toggleType = (t: string) => {
    const next = new Set(hiddenTypes);
    if (next.has(t)) next.delete(t);
    else next.add(t);
    setHiddenTypes(next);
    persist({ hidden: [...next] });
  };
  const securityTypes = useMemo(() => holdings.map((h) => h.security_type), [holdings]);
  const filtered = useMemo(
    () => (hiddenTypes.size ? holdings.filter((h) => !hiddenTypes.has(h.security_type)) : holdings),
    [holdings, hiddenTypes],
  );

  // Persist the full view on any control change (fire-and-forget). Always sends every field
  // (the PUT is a full replace), defaulting unspecified facets to current state — including
  // the valuation regime, so a grouping change never silently clears the saved mode.
  // visible_bands is always null: the column preset is session-only (landing = Overview),
  // and nulling it here also self-heals any band set persisted before 2026-07-08.
  const persist = (next: { grouping?: Mode; combine?: boolean; hidden?: string[]; valuation?: "live" | "settled" }) => {
    if (!portfolioId) return;
    void saveHoldingsViewAction(portfolioId, {
      grouping: next.grouping !== undefined ? next.grouping : mode,
      visible_bands: null,
      combine_by_account: next.combine !== undefined ? next.combine : byAccount,
      hidden_types: next.hidden !== undefined ? next.hidden : [...hiddenTypes],
      valuation: next.valuation !== undefined ? next.valuation : valuation,
    });
  };

  const changeMode = (m: Mode) => {
    setMode(m);
    persist({ grouping: m });
  };
  // Session-only by design — no persist (see header comment).
  const changeBands = (b: ColumnBand[]) => setVisibleGroups(b);
  const changeCombine = (on: boolean) => {
    if (on === byAccount) return;
    persist({ combine: on }); // remember across fresh loads
    const sp = new URLSearchParams(params.toString());
    sp.set("combine", on ? "accounts" : "combined"); // explicit both ways → URL wins this session
    router.push(`${pathname}?${sp.toString()}`);
  };
  const changeValuation = (liveOn: boolean) => {
    const next = liveOn ? "live" : "settled";
    if (next === valuation) return;
    persist({ valuation: next }); // remember across fresh loads
    const sp = new URLSearchParams(params.toString());
    sp.set("val", next); // explicit both ways → URL wins this session
    router.push(`${pathname}?${sp.toString()}`);
  };

  // "By account" grouping is only valid on the by-account data; in Combined mode it's not
  // offered and a stale selection falls back to asset-class.
  const modes = byAccount ? [ACCOUNT_MODE, ...BASE_MODES] : BASE_MODES;
  const effectiveMode: Mode = mode === "account" && !byAccount ? "asset" : mode;
  // Suppress the redundant Account column when sectioning by account — the heading names it.
  const accountColumn = byAccount && effectiveMode !== "account";

  // Column-band control. The grouper renders it directly UNDER the Portfolio total bar
  // (metron-ops#118+) so the column sets read as attached to the table, not stranded in the
  // top toolbar. Priced-only — bands are feed-gated in the cost-basis-only view.
  const columnControl = priced ? <ColumnPresetControl value={visibleGroups} onChange={changeBands} /> : null;

  return (
    <div className="space-y-3">
      {accounts && accounts.length > 0 && portfolioId ? (
        <BalanceByAccountPanel
          accounts={accounts}
          baseCurrency={baseCurrency}
          portfolioId={portfolioId}
          showDay={valuation === "live"}
        />
      ) : null}
      <div className="flex flex-wrap items-center gap-2">
        {liveAvailable && priced ? (
          <ValuationToggle live={valuation === "live"} sessionState={sessionState} onChange={changeValuation} />
        ) : null}
        {accounts && accounts.length > 0 && portfolioId ? (
          <AccountScopeChip accounts={accounts} baseCurrency={baseCurrency} portfolioId={portfolioId} />
        ) : null}
        <CombineToggle byAccount={byAccount} onChange={changeCombine} />
        <div className="inline-flex rounded-lg border border-line p-0.5 text-xs">
          {modes.map((m) => (
            <button
              key={m.key}
              type="button"
              onClick={() => changeMode(m.key)}
              className={SEG_BTN(effectiveMode === m.key)}
              aria-pressed={effectiveMode === m.key}
            >
              {m.label}
            </button>
          ))}
        </div>
      </div>
      <TypeFilterChips securityTypes={securityTypes} hidden={hiddenTypes} onToggle={toggleType} />
      {priced && filtered.length > 0 ? <HoldingsWhatIfPanel holdings={filtered} /> : null}
      {filtered.length === 0 ? (
        <p className="rounded-lg border border-line bg-surface px-4 py-3 text-sm text-muted">
          All instrument types are hidden — re-enable a type chip above to see holdings.
        </p>
      ) : effectiveMode === "account" ? (
        <GroupedByAccount
          holdings={filtered}
          baseCurrency={baseCurrency}
          priced={priced}
          portfolioId={portfolioId}
          visibleBands={visibleGroups}
          belowTotal={columnControl}
        />
      ) : effectiveMode === "asset" ? (
        <GroupedHoldings
          holdings={filtered}
          baseCurrency={baseCurrency}
          priced={priced}
          portfolioId={portfolioId}
          visibleBands={visibleGroups}
          accountColumn={accountColumn}
          belowTotal={columnControl}
        />
      ) : (
        <GroupedByClassification
          holdings={filtered}
          baseCurrency={baseCurrency}
          priced={priced}
          medians={medians}
          portfolioId={portfolioId}
          visibleBands={visibleGroups}
          accountColumn={accountColumn}
          belowTotal={columnControl}
        />
      )}
    </div>
  );
}
