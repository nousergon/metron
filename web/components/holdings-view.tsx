"use client";

// Holdings toolbar + grouping switch. Three controls (metron-ops#114): the COMBINE toggle —
// Combined (one row per ticker) vs By account (one row per account-position, +Account
// column) — the GROUPING — "By asset class" / "By sector → country" / "By account" — and the
// COLUMN PRESET (which metric bands show, over the always-on position spine). Combine is
// URL-driven (it changes the holdings DATA shape, fetched server-side); grouping + preset are
// client-side presentational state. ALL THREE persist to InvestorPreferences (metron-ops#114)
// so the view survives reloads — hydrated from the saved values, saved fire-and-forget on
// change.

import { useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { GroupedByAccount } from "@/components/grouped-by-account";
import { GroupedByClassification } from "@/components/grouped-by-classification";
import { GroupedHoldings } from "@/components/grouped-holdings";
import { ColumnPresetControl, DEFAULT_VISIBLE_GROUPS } from "@/components/holdings-column-presets";
import { METRIC_GROUP_ORDER, type MetricGroup } from "@/components/holdings-table";
import { saveHoldingsViewAction } from "@/app/portfolios/[id]/actions";
import type { Holding, ValuationMedians } from "@/lib/api";

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

/** Saved band names → a valid, canonical-ordered MetricGroup[] (defaults to the lean set). */
function initialBands(saved: string[] | null): MetricGroup[] {
  if (!saved || saved.length === 0) return DEFAULT_VISIBLE_GROUPS;
  const valid = METRIC_GROUP_ORDER.filter((g) => saved.includes(g));
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

export function HoldingsView({
  holdings,
  baseCurrency,
  priced,
  medians,
  portfolioId,
  byAccount = false,
  savedGrouping = null,
  savedBands = null,
}: {
  holdings: Holding[];
  baseCurrency: string;
  priced: boolean;
  medians: ValuationMedians | null;
  portfolioId?: string;
  /** Uncombined view — holdings carry per-account rows; render the Account column. */
  byAccount?: boolean;
  /** Saved view (metron-ops#114) — hydrate the grouping + visible bands. */
  savedGrouping?: string | null;
  savedBands?: string[] | null;
}) {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const [mode, setMode] = useState<Mode>(() => initialMode(savedGrouping));
  const [visibleGroups, setVisibleGroups] = useState<MetricGroup[]>(() => initialBands(savedBands));

  // Persist the full view on any control change (fire-and-forget). Always sends all three
  // fields (the PUT is a full replace), defaulting unspecified facets to current state.
  const persist = (next: { grouping?: Mode; bands?: MetricGroup[]; combine?: boolean }) => {
    if (!portfolioId) return;
    void saveHoldingsViewAction(portfolioId, {
      grouping: next.grouping !== undefined ? next.grouping : mode,
      visible_bands: next.bands !== undefined ? next.bands : visibleGroups,
      combine_by_account: next.combine !== undefined ? next.combine : byAccount,
    });
  };

  const changeMode = (m: Mode) => {
    setMode(m);
    persist({ grouping: m });
  };
  const changeBands = (b: MetricGroup[]) => {
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

  // "By account" grouping is only valid on the by-account data; in Combined mode it's not
  // offered and a stale selection falls back to asset-class.
  const modes = byAccount ? [ACCOUNT_MODE, ...BASE_MODES] : BASE_MODES;
  const effectiveMode: Mode = mode === "account" && !byAccount ? "asset" : mode;
  // Suppress the redundant Account column when sectioning by account — the heading names it.
  const accountColumn = byAccount && effectiveMode !== "account";

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2">
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
        {/* Column presets only matter in the priced view (metric bands are feed-gated). */}
        {priced ? <ColumnPresetControl value={visibleGroups} onChange={changeBands} /> : null}
      </div>
      {effectiveMode === "account" ? (
        <GroupedByAccount
          holdings={holdings}
          baseCurrency={baseCurrency}
          priced={priced}
          portfolioId={portfolioId}
          visibleMetricGroups={visibleGroups}
        />
      ) : effectiveMode === "asset" ? (
        <GroupedHoldings
          holdings={holdings}
          baseCurrency={baseCurrency}
          priced={priced}
          portfolioId={portfolioId}
          visibleMetricGroups={visibleGroups}
          accountColumn={accountColumn}
        />
      ) : (
        <GroupedByClassification
          holdings={holdings}
          baseCurrency={baseCurrency}
          priced={priced}
          medians={medians}
          portfolioId={portfolioId}
          visibleMetricGroups={visibleGroups}
          accountColumn={accountColumn}
        />
      )}
    </div>
  );
}
