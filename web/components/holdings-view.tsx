"use client";

// Holdings toolbar + grouping switch. Three controls (metron-ops#114): the COMBINE toggle —
// Combined (one row per ticker) vs By account (one row per account-position, +Account
// column) — the GROUPING — "By asset class" vs "By sector → country" — and the COLUMN PRESET
// (which metric bands show, over the always-on position spine). Combine is URL-driven (it
// changes the holdings DATA shape, fetched server-side); grouping + preset are client-side
// presentational state.

import { useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { GroupedByAccount } from "@/components/grouped-by-account";
import { GroupedByClassification } from "@/components/grouped-by-classification";
import { GroupedHoldings } from "@/components/grouped-holdings";
import { ColumnPresetControl, DEFAULT_VISIBLE_GROUPS } from "@/components/holdings-column-presets";
import type { MetricGroup } from "@/components/holdings-table";
import type { Holding, ValuationMedians } from "@/lib/api";

type Mode = "account" | "asset" | "classification";

// "By account" sections the per-account rows by account; only meaningful (and only offered)
// on the by-account data, so it's appended to the grouping control when Combine = By account.
const ACCOUNT_MODE: { key: Mode; label: string } = { key: "account", label: "By account" };
const BASE_MODES: { key: Mode; label: string }[] = [
  { key: "asset", label: "By asset class" },
  { key: "classification", label: "By sector → country" },
];

const SEG_BTN = (active: boolean) =>
  `rounded-md px-2.5 py-1 transition ${active ? "bg-surface font-medium text-ink" : "text-muted hover:text-ink"}`;

/** Combined vs By-account, driven by the `?combine=accounts` URL param (server re-fetches
 *  the matching holdings shape). Preserves the rest of the query (e.g. account_id scope). */
function CombineToggle({ byAccount }: { byAccount: boolean }) {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const setCombine = (on: boolean) => {
    if (on === byAccount) return;
    const sp = new URLSearchParams(params.toString());
    if (on) sp.set("combine", "accounts");
    else sp.delete("combine");
    const qs = sp.toString();
    router.push(qs ? `${pathname}?${qs}` : pathname);
  };
  return (
    <div className="inline-flex rounded-lg border border-line p-0.5 text-xs">
      <button type="button" onClick={() => setCombine(false)} className={SEG_BTN(!byAccount)} aria-pressed={!byAccount}>
        Combined
      </button>
      <button type="button" onClick={() => setCombine(true)} className={SEG_BTN(byAccount)} aria-pressed={byAccount}>
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
}: {
  holdings: Holding[];
  baseCurrency: string;
  priced: boolean;
  medians: ValuationMedians | null;
  portfolioId?: string;
  /** Uncombined view — holdings carry per-account rows; render the Account column. */
  byAccount?: boolean;
}) {
  const [mode, setMode] = useState<Mode>("asset");
  const [visibleGroups, setVisibleGroups] = useState<MetricGroup[]>(DEFAULT_VISIBLE_GROUPS);

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
          <CombineToggle byAccount={byAccount} />
          <div className="inline-flex rounded-lg border border-line p-0.5 text-xs">
            {modes.map((m) => (
              <button
                key={m.key}
                type="button"
                onClick={() => setMode(m.key)}
                className={SEG_BTN(effectiveMode === m.key)}
                aria-pressed={effectiveMode === m.key}
              >
                {m.label}
              </button>
            ))}
          </div>
        </div>
        {/* Column presets only matter in the priced view (metric bands are feed-gated). */}
        {priced ? <ColumnPresetControl value={visibleGroups} onChange={setVisibleGroups} /> : null}
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
