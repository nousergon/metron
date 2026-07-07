"use client";

// Holdings toolbar + grouping switch. Three controls (metron-ops#114): the COMBINE toggle —
// Combined (one row per ticker) vs By account (one row per account-position, +Account
// column) — the GROUPING — "By asset class" / "By sector → country" / "By account" — and the
// COLUMN PRESET (which metric bands show, over the always-on position spine). Combine is
// URL-driven (it changes the holdings DATA shape, fetched server-side); grouping + preset are
// client-side presentational state. ALL THREE persist to InvestorPreferences (metron-ops#114)
// so the view survives reloads — hydrated from the saved values, saved fire-and-forget on
// change.

import { useMemo, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { GroupedByAccount } from "@/components/grouped-by-account";
import { GroupedByClassification } from "@/components/grouped-by-classification";
import { GroupedHoldings } from "@/components/grouped-holdings";
import { TypeFilterChips } from "@/components/holdings-type-filter";
import { AccountScopeChip } from "@/components/account-scope-chip";
import { ColumnPresetControl, DEFAULT_VISIBLE_GROUPS } from "@/components/holdings-column-presets";
import { BAND_ORDER, type ColumnBand } from "@/components/holdings-table";
import { saveHoldingsViewAction } from "@/app/portfolios/[id]/actions";
import type { Account, Holding, ValuationMedians } from "@/lib/api";

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

/** Saved band names → a valid, canonical-ordered ColumnBand[] (defaults to the lean set). */
function initialBands(saved: string[] | null): ColumnBand[] {
  if (!saved || saved.length === 0) return DEFAULT_VISIBLE_GROUPS;
  const valid = BAND_ORDER.filter((g) => saved.includes(g));
  return valid.length ? valid : DEFAULT_VISIBLE_GROUPS;
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
  savedBands = null,
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
  /** Saved view (metron-ops#114/#115) — hydrate grouping / visible bands / hidden types. */
  savedGrouping?: string | null;
  savedBands?: string[] | null;
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
  const [visibleGroups, setVisibleGroups] = useState<ColumnBand[]>(() => initialBands(savedBands));
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
  const persist = (next: { grouping?: Mode; bands?: ColumnBand[]; combine?: boolean; hidden?: string[]; valuation?: "live" | "settled" }) => {
    if (!portfolioId) return;
    void saveHoldingsViewAction(portfolioId, {
      grouping: next.grouping !== undefined ? next.grouping : mode,
      visible_bands: next.bands !== undefined ? next.bands : visibleGroups,
      combine_by_account: next.combine !== undefined ? next.combine : byAccount,
      hidden_types: next.hidden !== undefined ? next.hidden : [...hiddenTypes],
      valuation: next.valuation !== undefined ? next.valuation : valuation,
    });
  };

  const changeMode = (m: Mode) => {
    setMode(m);
    persist({ grouping: m });
  };
  const changeBands = (b: ColumnBand[]) => {
    setVisibleGroups(b);
    persist({ bands: b });
  };
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
