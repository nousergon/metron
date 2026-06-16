import { acctParams, getAccounts, getHoldings, getIncome, getPlugins, getPortfolio, getSummary, MetronApiError, type Portfolio, type PluginNav } from "@/lib/api";
import { moneyWhole, signClass, signedMoneyWhole } from "@/lib/format";
import { Empty, Section, StatCard, Table } from "@/components/ui";
import { AccountPanel } from "@/components/account-panel";
import { PortfolioNav } from "@/components/portfolio-nav";
import { TierSimulator } from "@/components/tier-simulator";
import { GroupedHoldings } from "@/components/grouped-holdings";
import { RefreshPrices } from "@/components/refresh-prices";
import { RenamePortfolio } from "@/components/rename-portfolio";
import { loadEntitlements, toFeatureStates } from "@/lib/entitlements";
import { requireTenantId } from "@/lib/session";
import { resolveAccountIds } from "@/lib/selection";

export const dynamic = "force-dynamic";

export default async function PortfolioPage({
  params,
  searchParams,
}: {
  params: { id: string };
  searchParams: { account_id?: string | string[] };
}) {
  const { id } = params;
  const tenantId = await requireTenantId();

  // The account-panel selection (repeatable ?account_id=); empty = whole portfolio.
  // URL selection wins; with none, the saved panel selection is applied (redirect).
  const accountIds = await resolveAccountIds(tenantId, id, `/portfolios/${id}`, searchParams.account_id);
  const scoped = accountIds.length > 0;
  // Carry the selection onto the cross-page nav links so it persists.
  const navQuery = acctParams(accountIds);

  let portfolio: Portfolio, summary, holdings, income, accounts;
  try {
    [portfolio, summary, holdings, income, accounts] = await Promise.all([
      getPortfolio(tenantId, id),
      getSummary(tenantId, id, accountIds),
      getHoldings(tenantId, id, accountIds),
      getIncome(tenantId, id, accountIds),
      getAccounts(tenantId, id),
    ]);
  } catch (e) {
    if (e instanceof MetronApiError && e.status === 404) {
      return <Empty>Portfolio not found.</Empty>;
    }
    return <Empty>Couldn&apos;t load this portfolio. Is the backend running?</Empty>;
  }

  const ccy = summary.base_currency;
  const priced = summary.market_value != null;

  // Premium nav (metron-ops). Best-effort + always empty on the public tier — a
  // failure here must never break the core portfolio view.
  let plugins: PluginNav[] = [];
  try {
    plugins = await getPlugins(tenantId);
  } catch {
    plugins = [];
  }

  // Product-tier entitlements (drives the nav lock state + the owner-only tier
  // simulator). The preview cookies are honored server-side ONLY when the
  // simulator is enabled; on the public product they're ignored. Best-effort.
  const entitlements = await loadEntitlements(tenantId);
  const featureStates = toFeatureStates(entitlements);

  return (
    <div>
      <PortfolioNav portfolioId={id} name={portfolio.name} navQuery={navQuery} plugins={plugins} featureStates={featureStates} />
      {entitlements ? <TierSimulator entitlements={entitlements} /> : null}

      <div className="mt-3">
        <RenamePortfolio portfolioId={id} name={portfolio.name} />
      </div>

      <Section title="Accounts">
        <AccountPanel accounts={accounts} baseCurrency={ccy} portfolioId={id} />
        {scoped ? (
          <p className="mt-2 text-xs text-muted">
            Showing {summary.n_accounts} of {accounts.length} account{accounts.length === 1 ? "" : "s"} — totals,
            holdings, income, Risk and Attribution below reflect this selection. (Performance stays whole-portfolio.)
          </p>
        ) : null}
      </Section>

      <div className="mt-6 grid grid-cols-2 gap-3 sm:grid-cols-4">
        {priced ? (
          <>
            <StatCard
              label="Market value"
              value={moneyWhole(summary.market_value as number, ccy)}
              hint={`cost ${moneyWhole(summary.total_cost_basis, ccy)}`}
            />
            <StatCard
              label="Unrealized"
              value={signedMoneyWhole(summary.unrealized_gain as number, ccy)}
              valueClass={signClass(summary.unrealized_gain as number)}
              hint="vs cost basis"
            />
          </>
        ) : (
          <StatCard label="Cost basis" value={moneyWhole(summary.total_cost_basis, ccy)} hint={`${summary.n_holdings} holdings`} />
        )}
        <StatCard
          label="Realized gains"
          value={signedMoneyWhole(summary.realized_total, ccy)}
          valueClass={signClass(summary.realized_total)}
          hint="short + long term"
        />
        <StatCard label="Income" value={moneyWhole(summary.dividends + summary.interest, ccy)} hint="dividends + interest" />
        <StatCard label="Accounts" value={String(summary.n_accounts)} />
      </div>

      {summary.n_unconverted > 0 ? (
        <p className="mt-2 text-xs text-muted">
          {summary.n_unconverted} foreign holding{summary.n_unconverted === 1 ? "" : "s"} excluded from the{" "}
          {ccy} totals — no FX rate cached yet. Refresh prices to fetch it.
        </p>
      ) : null}

      <Section title="Holdings" note={priced ? `all values in ${ccy} · market value from last EOD close` : "cost basis — refresh for market value"}>
        <div className="mb-3">
          <RefreshPrices portfolioId={id} />
        </div>
        {holdings.length === 0 ? (
          <Empty>No open positions.</Empty>
        ) : (
          <GroupedHoldings holdings={holdings} baseCurrency={ccy} priced={priced} />
        )}
      </Section>

      <Section title="Income by year">
        {income.length === 0 ? (
          <Empty>No realized income yet.</Empty>
        ) : (
          <Table head={["Year", "Short-term", "Long-term", "Dividends", "Interest", "Taxable income"]}>
            {income.map((y) => (
              <tr key={y.year} className="border-b border-line last:border-0">
                <td className="px-4 py-2 font-medium">{y.year}</td>
                <td className={`px-4 py-2 text-right tabular-nums ${signClass(y.realized_st)}`}>
                  {signedMoneyWhole(y.realized_st, ccy)}
                </td>
                <td className={`px-4 py-2 text-right tabular-nums ${signClass(y.realized_lt)}`}>
                  {signedMoneyWhole(y.realized_lt, ccy)}
                </td>
                <td className="px-4 py-2 text-right tabular-nums">{moneyWhole(y.dividends, ccy)}</td>
                <td className="px-4 py-2 text-right tabular-nums">{moneyWhole(y.interest, ccy)}</td>
                <td className="px-4 py-2 text-right font-medium tabular-nums">{moneyWhole(y.taxable_income, ccy)}</td>
              </tr>
            ))}
          </Table>
        )}
      </Section>
    </div>
  );
}
