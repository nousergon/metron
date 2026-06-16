import { acctParams, getIncome, getSummary, getTax, MetronApiError } from "@/lib/api";
import { isoDate, money, moneyWhole, quantity, signClass, signedMoney, signedMoneyWhole } from "@/lib/format";
import { Empty, Section, StatCard, Table } from "@/components/ui";
import { PortfolioNav } from "@/components/portfolio-nav";
import { navFeatureStates } from "@/lib/entitlements";
import { requireTenantId } from "@/lib/session";
import { resolveAccountIds } from "@/lib/selection";

export const dynamic = "force-dynamic";

export default async function TaxPage({
  params,
  searchParams,
}: {
  params: { id: string };
  searchParams: { account_id?: string | string[] };
}) {
  const { id } = params;
  const tenantId = await requireTenantId();
  const featureStates = await navFeatureStates(tenantId);

  // URL selection wins; with none, the saved panel selection is applied (redirect).
  const accountIds = await resolveAccountIds(tenantId, id, `/portfolios/${id}/tax`, searchParams.account_id);
  const navQuery = acctParams(accountIds);

  let taxData, summary, income;
  try {
    [taxData, summary, income] = await Promise.all([
      getTax(tenantId, id, accountIds),
      getSummary(tenantId, id, accountIds),
      getIncome(tenantId, id, accountIds, true), // taxable accounts only
    ]);
  } catch (e) {
    if (e instanceof MetronApiError && e.status === 404) {
      return <Empty>Portfolio not found.</Empty>;
    }
    return <Empty>Couldn&apos;t load tax. Is the backend running?</Empty>;
  }

  const ccy = summary.base_currency;
  const priced = taxData.unrealized_total != null;

  return (
    <div>
      <PortfolioNav portfolioId={id} navQuery={navQuery} featureStates={featureStates} />

      <h1 className="mt-3 text-lg font-semibold">Tax</h1>
      <p className="text-sm text-muted">
        Per-lot holding-period term and unrealized P&amp;L (at the last close, in {ccy}), with harvestable losses
        flagged. Taxable accounts only. Descriptive, not advice.
      </p>
      {taxData.n_accounts_excluded > 0 ? (
        <p className="mt-1 text-xs text-muted">
          {taxData.n_accounts_excluded} tax-advantaged account{taxData.n_accounts_excluded === 1 ? "" : "s"} (IRA /
          401(k) / Roth …) excluded — gains there are never taxed.
        </p>
      ) : null}

      {priced ? (
        <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
          <StatCard
            label="Unrealized (short-term)"
            value={signedMoney(taxData.unrealized_st as number, ccy)}
            valueClass={signClass(taxData.unrealized_st as number)}
          />
          <StatCard
            label="Unrealized (long-term)"
            value={signedMoney(taxData.unrealized_lt as number, ccy)}
            valueClass={signClass(taxData.unrealized_lt as number)}
          />
          <StatCard
            label="Total unrealized"
            value={signedMoney(taxData.unrealized_total as number, ccy)}
            valueClass={signClass(taxData.unrealized_total as number)}
          />
          <StatCard
            label="Harvestable loss"
            value={money(taxData.harvestable_loss ?? 0, ccy)}
            hint="available to harvest"
          />
        </div>
      ) : (
        <div className="mt-4">
          <Empty>Refresh prices on the portfolio page to value lots and surface harvestable losses.</Empty>
        </div>
      )}

      <Section title="Lots" note={`${taxData.n_lots} open · cost basis & term are price-free`}>
        {taxData.lots.length === 0 ? (
          <Empty>No open lots.</Empty>
        ) : (
          <Table head={["Ticker", "Ccy", "Opened", "Term", "Quantity", "Cost basis", "Market value", "Unrealized", "Harvest"]}>
            {taxData.lots.map((l, i) => (
              <tr key={`${l.ticker}-${l.open_date}-${i}`} className="border-b border-line last:border-0">
                <td className="px-4 py-2 font-medium">{l.ticker}</td>
                <td className="px-4 py-2 text-muted">{l.currency}</td>
                <td className="px-4 py-2 text-right text-muted">{isoDate(l.open_date)}</td>
                <td className="px-4 py-2 text-right text-muted">{l.term === "Long-term" ? "LT" : l.term === "Short-term" ? "ST" : "?"}</td>
                <td className="px-4 py-2 text-right tabular-nums">{quantity(l.quantity)}</td>
                <td className="px-4 py-2 text-right tabular-nums">{money(l.cost_basis, l.currency)}</td>
                <td className="px-4 py-2 text-right tabular-nums">{l.market_value != null ? money(l.market_value, ccy) : "—"}</td>
                <td className={`px-4 py-2 text-right tabular-nums ${signClass(l.unrealized_gain ?? 0)}`}>
                  {l.unrealized_gain != null ? signedMoney(l.unrealized_gain, ccy) : "—"}
                </td>
                <td className="px-4 py-2 text-right tabular-nums">
                  {(l.harvestable_loss ?? 0) > 0 ? (
                    <span className="text-negative">{money(l.harvestable_loss as number, ccy)}</span>
                  ) : (
                    "—"
                  )}
                </td>
              </tr>
            ))}
          </Table>
        )}
      </Section>

      <Section title="Realized income by year" note="taxable accounts only — short/long-term gains, dividends, interest">
        {income.length === 0 ? (
          <Empty>No taxable realized income yet.</Empty>
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
