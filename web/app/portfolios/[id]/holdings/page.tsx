import { acctParams, getAccounts, getHoldings, getHoldingsPerformanceSeries, getSummary, MetronApiError, type HoldingsPerfSeries } from "@/lib/api";
import { Empty, Section } from "@/components/ui";
import { AccountPanel } from "@/components/account-panel";
import { GroupedHoldings } from "@/components/grouped-holdings";
import { HoldingsPerfChart } from "@/components/holdings-perf-chart";
import { RefreshPrices } from "@/components/refresh-prices";
import { PortfolioNav } from "@/components/portfolio-nav";
import { loadEntitlements, navFeatureStates } from "@/lib/entitlements";
import { requireTenantId } from "@/lib/session";
import { resolveAccountIds } from "@/lib/selection";

export const dynamic = "force-dynamic";

// Holdings — the position-level detail, separated from the Overview dashboard
// (metron-ops#64). Accounts are (de)activated HERE to see the effect on specific
// holdings; the selection persists and the Overview's aggregate metrics follow it.
export default async function HoldingsPage({
  params,
  searchParams,
}: {
  params: { id: string };
  searchParams: { account_id?: string | string[] };
}) {
  const { id } = params;
  const tenantId = await requireTenantId();
  const featureStates = await navFeatureStates(tenantId);

  const accountIds = await resolveAccountIds(tenantId, id, `/portfolios/${id}/holdings`, searchParams.account_id);
  const scoped = accountIds.length > 0;
  const navQuery = acctParams(accountIds);

  let summary, holdings, accounts;
  try {
    [summary, holdings, accounts] = await Promise.all([
      getSummary(tenantId, id, accountIds),
      getHoldings(tenantId, id, accountIds),
      getAccounts(tenantId, id),
    ]);
  } catch (e) {
    if (e instanceof MetronApiError && e.status === 404) {
      return <Empty>Portfolio not found.</Empty>;
    }
    return <Empty>Couldn&apos;t load holdings. Is the backend running?</Empty>;
  }

  const ccy = summary.base_currency;
  const priced = summary.market_value != null;
  const entitlements = await loadEntitlements(tenantId);

  // Per-account performance lines above the table (metron-ops#78) — best-effort, scoped to
  // the active account selection. Benchmark overlays are feed-gated server-side. Shown once
  // at least one account has ≥2 recorded NAV snapshots (a line needs two points).
  let perfSeries: HoldingsPerfSeries | null = null;
  try {
    perfSeries = await getHoldingsPerformanceSeries(tenantId, id, accountIds);
  } catch {
    perfSeries = null;
  }
  const showChart = (perfSeries?.accounts.length ?? 0) > 0;

  return (
    <div>
      <PortfolioNav portfolioId={id} navQuery={navQuery} featureStates={featureStates} />

      <h1 className="mt-3 text-lg font-semibold">Holdings</h1>
      <p className="text-sm text-muted">
        (De)activate accounts to see how they affect the positions below. The selection persists and the Overview
        metrics follow it.
      </p>

      <Section title="Accounts">
        <AccountPanel accounts={accounts} baseCurrency={ccy} portfolioId={id} />
        {scoped ? (
          <p className="mt-2 text-xs text-muted">
            Showing {summary.n_accounts} of {accounts.length} account{accounts.length === 1 ? "" : "s"} — the holdings
            below reflect this selection.
          </p>
        ) : null}
      </Section>

      {/* Per-account performance lines above the table (metron-ops#78). */}
      {showChart && perfSeries ? (
        <Section title="Performance">
          <HoldingsPerfChart
            accounts={perfSeries.accounts}
            benchmarks={perfSeries.benchmarks}
            benchmarksAvailable={perfSeries.benchmarks_available}
          />
        </Section>
      ) : null}

      <Section title="Holdings" note={priced ? `all values in ${ccy} · market value from last EOD close` : "cost basis — refresh for market value"}>
        <div className="mb-3">
          <RefreshPrices portfolioId={id} feedOn={entitlements?.feed_enabled} />
        </div>
        {holdings.length === 0 ? (
          <Empty>No open positions.</Empty>
        ) : (
          <GroupedHoldings holdings={holdings} baseCurrency={ccy} priced={priced} portfolioId={id} />
        )}
      </Section>
    </div>
  );
}
