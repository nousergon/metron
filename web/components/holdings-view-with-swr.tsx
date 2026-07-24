"use client";

// SWR-backed holdings view (metron-ops#232) — the server component's SSR data seeds
// `fallbackData` so first paint is instant; subsequent mutations call `mutate()` on the
// holdings key to revalidate just this cache entry instead of a blanket `router.refresh()`.

import { useHoldings } from "@/lib/use-holdings";
import { HoldingsView } from "@/components/holdings-view";
import type { Account, Holding, ValuationMedians } from "@/lib/api";

export function HoldingsViewWithSwr({
  portfolioId,
  accountIds,
  byAccount,
  valuation,
  initialHoldings,
  baseCurrency,
  priced,
  medians,
  savedGrouping,
  savedHiddenTypes,
  liveAvailable,
  sessionState,
  accounts,
  selectedAccountIds,
}: {
  portfolioId: string;
  accountIds: string[];
  byAccount: boolean;
  valuation: "live" | "settled";
  initialHoldings: Holding[];
  baseCurrency: string;
  priced: boolean;
  medians: ValuationMedians | null;
  savedGrouping: string | null;
  savedHiddenTypes: string[] | null;
  liveAvailable: boolean;
  sessionState: "live" | "recap" | "closed";
  accounts?: Account[];
  selectedAccountIds?: string[];
}) {
  const { data: holdings = initialHoldings } = useHoldings(
    portfolioId,
    accountIds,
    byAccount,
    valuation,
    initialHoldings,
  );

  return (
    <HoldingsView
      holdings={holdings ?? initialHoldings}
      baseCurrency={baseCurrency}
      priced={priced}
      medians={medians}
      portfolioId={portfolioId}
      byAccount={byAccount}
      savedGrouping={savedGrouping}
      savedHiddenTypes={savedHiddenTypes}
      valuation={valuation}
      liveAvailable={liveAvailable}
      sessionState={sessionState}
      accounts={accounts}
      selectedAccountIds={selectedAccountIds}
    />
  );
}
