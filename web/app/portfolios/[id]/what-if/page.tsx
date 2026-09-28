import { acctParams, getHoldings, getSummary, MetronApiError, type Holding } from "@/lib/api";
import { Empty, Section } from "@/components/ui";
import { PortfolioNav } from "@/components/portfolio-nav";
import { HoldingsWhatIfPanel } from "@/components/holdings-whatif-panel";
import { IfSoldTaxPanel } from "@/components/if-sold-tax-panel";
import { navFeatureStates } from "@/lib/entitlements";
import { requireApiAuth } from "@/lib/session";
import { resolveAccountIds } from "@/lib/selection";

export const dynamic = "force-dynamic";

// What-if — the two hypothetical sandboxes, moved off the Holdings landing page so the
// landing stays an at-a-glance view (Brian, 2026-09-28):
//   - "What-if: try hypothetical weights" (HoldingsWhatIfPanel, metron-ops#171) — client-side
//     only; measures a weight set the user types, over the holdings at the settled close.
//   - "If sold — tax estimate" (IfSoldTaxPanel, metron-ops#208) — the read-only
//     fetchIfSoldAction server action (../tax-whatif-action.ts), unchanged.
// Scoped like Holdings: the `?account_id=` selection the nav carries, else the whole
// portfolio (no saved-selection redirect — parity with the page these panels came from).
export default async function WhatIfPage(
  props: {
    params: Promise<{ id: string }>;
    searchParams: Promise<{ account_id?: string | string[] }>;
  }
) {
  const searchParams = await props.searchParams;
  const params = await props.params;
  const { id } = params;
  const apiAuth = await requireApiAuth();

  const [featureStates, accountIds] = await Promise.all([
    navFeatureStates(apiAuth),
    resolveAccountIds(apiAuth, id, `/portfolios/${id}/what-if`, searchParams.account_id, { applySaved: false }),
  ]);
  const navQuery = acctParams(accountIds);

  let holdings: Holding[];
  let priced: boolean;
  try {
    const [summary, rows] = await Promise.all([getSummary(apiAuth, id, accountIds), getHoldings(apiAuth, id, accountIds)]);
    holdings = rows;
    priced = summary.market_value != null;
  } catch (e) {
    if (e instanceof MetronApiError && e.status === 404) {
      return <Empty>Portfolio not found.</Empty>;
    }
    return <Empty>Couldn&apos;t load holdings. Is the backend running?</Empty>;
  }

  const tickers = [...new Set(holdings.map((h) => h.ticker))].sort();

  return (
    <div>
      <PortfolioNav portfolioId={id} navQuery={navQuery} featureStates={featureStates} />

      <h1 className="mt-3 text-lg font-semibold">What-if</h1>
      <p className="text-sm text-muted">
        Hypotheticals you describe, measured against your current holdings at the settled close. Nothing here changes
        your holdings or is saved.
      </p>

      {holdings.length === 0 ? (
        <Section title="Holdings">
          <Empty>No open positions.</Empty>
        </Section>
      ) : (
        <>
          <Section title="Hypothetical weights" note="concentration, sectors, valuation — current vs hypothetical">
            {priced ? (
              <HoldingsWhatIfPanel holdings={holdings} defaultOpen />
            ) : (
              <Empty>Weights need market values — refresh prices on Holdings first.</Empty>
            )}
          </Section>
          <Section title="If sold" note="tax estimate for a hypothetical sale">
            <IfSoldTaxPanel portfolioId={id} tickers={tickers} accountIds={accountIds} />
          </Section>
        </>
      )}
    </div>
  );
}
