import Link from "next/link";
import { getAccounts, getHoldings, getIncome, getSummary, MetronApiError } from "@/lib/api";
import { money, percent, quantity, signClass, signedMoney } from "@/lib/format";
import { Empty, Section, StatCard, Table } from "@/components/ui";
import { ImportPanel } from "@/components/import-panel";
import { RefreshPrices } from "@/components/refresh-prices";
import { requireTenantId } from "@/lib/session";

export const dynamic = "force-dynamic";

export default async function PortfolioPage({ params }: { params: { id: string } }) {
  const { id } = params;
  const tenantId = await requireTenantId();

  let summary, holdings, income, accounts;
  try {
    [summary, holdings, income, accounts] = await Promise.all([
      getSummary(tenantId, id),
      getHoldings(tenantId, id),
      getIncome(tenantId, id),
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

  return (
    <div>
      <div className="flex items-baseline justify-between">
        <Link href="/" className="text-sm text-muted hover:text-ink">
          ← Portfolios
        </Link>
        <div className="flex gap-4">
          <Link href={`/portfolios/${id}/performance`} className="text-sm text-muted hover:text-ink">
            Performance →
          </Link>
          <Link href={`/portfolios/${id}/risk`} className="text-sm text-muted hover:text-ink">
            Risk →
          </Link>
          <Link href={`/portfolios/${id}/tax`} className="text-sm text-muted hover:text-ink">
            Tax →
          </Link>
          <Link href={`/portfolios/${id}/transactions`} className="text-sm text-muted hover:text-ink">
            Transactions &amp; realized →
          </Link>
        </div>
      </div>

      <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
        {priced ? (
          <>
            <StatCard
              label="Market value"
              value={money(summary.market_value as number, ccy)}
              hint={`cost ${money(summary.total_cost_basis, ccy)}`}
            />
            <StatCard
              label="Unrealized"
              value={signedMoney(summary.unrealized_gain as number, ccy)}
              valueClass={signClass(summary.unrealized_gain as number)}
              hint="vs cost basis"
            />
          </>
        ) : (
          <StatCard label="Cost basis" value={money(summary.total_cost_basis, ccy)} hint={`${summary.n_holdings} holdings`} />
        )}
        <StatCard
          label="Realized gains"
          value={signedMoney(summary.realized_total, ccy)}
          valueClass={signClass(summary.realized_total)}
          hint="short + long term"
        />
        <StatCard label="Income" value={money(summary.dividends + summary.interest, ccy)} hint="dividends + interest" />
        <StatCard label="Accounts" value={String(summary.n_accounts)} />
      </div>

      <Section title="Import" note="CSV / OFX / IBKR Flex — $0, no aggregator">
        <ImportPanel portfolioId={id} />
      </Section>

      <Section title="Holdings" note={priced ? "market value from last EOD close" : "cost basis — refresh for market value"}>
        <div className="mb-3">
          <RefreshPrices portfolioId={id} />
        </div>
        {holdings.length === 0 ? (
          <Empty>No open positions.</Empty>
        ) : (
          <Table
            head={
              priced
                ? ["Ticker", "Quantity", "Avg cost", "Cost basis", "Last", "Market value", "Unrealized"]
                : ["Ticker", "Quantity", "Avg cost", "Cost basis"]
            }
          >
            {holdings.map((h) => (
              <tr key={h.ticker} className="border-b border-line last:border-0">
                <td className="px-4 py-2 font-medium">{h.ticker}</td>
                <td className="px-4 py-2 text-right tabular-nums">{quantity(h.quantity)}</td>
                <td className="px-4 py-2 text-right tabular-nums">{money(h.avg_cost, ccy)}</td>
                <td className="px-4 py-2 text-right tabular-nums">{money(h.cost_basis, ccy)}</td>
                {priced ? (
                  <>
                    <td className="px-4 py-2 text-right tabular-nums text-muted">
                      {h.last_price != null ? money(h.last_price, ccy) : "—"}
                    </td>
                    <td className="px-4 py-2 text-right tabular-nums">
                      {h.market_value != null ? money(h.market_value, ccy) : "—"}
                    </td>
                    <td className={`px-4 py-2 text-right tabular-nums ${signClass(h.unrealized_gain ?? 0)}`}>
                      {h.unrealized_gain != null ? (
                        <>
                          {signedMoney(h.unrealized_gain, ccy)}
                          {h.unrealized_pct != null ? (
                            <span className="ml-1 text-xs">({percent(h.unrealized_pct)})</span>
                          ) : null}
                        </>
                      ) : (
                        "—"
                      )}
                    </td>
                  </>
                ) : null}
              </tr>
            ))}
          </Table>
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
                  {signedMoney(y.realized_st, ccy)}
                </td>
                <td className={`px-4 py-2 text-right tabular-nums ${signClass(y.realized_lt)}`}>
                  {signedMoney(y.realized_lt, ccy)}
                </td>
                <td className="px-4 py-2 text-right tabular-nums">{money(y.dividends, ccy)}</td>
                <td className="px-4 py-2 text-right tabular-nums">{money(y.interest, ccy)}</td>
                <td className="px-4 py-2 text-right font-medium tabular-nums">{money(y.taxable_income, ccy)}</td>
              </tr>
            ))}
          </Table>
        )}
      </Section>

      <Section title="Accounts">
        {accounts.length === 0 ? (
          <Empty>No connected accounts.</Empty>
        ) : (
          <Table head={["Account", "Broker", "Currency"]}>
            {accounts.map((a) => (
              <tr key={a.account_id} className="border-b border-line last:border-0 hover:bg-slate-50">
                <td className="px-4 py-2 font-medium">
                  <Link href={`/portfolios/${id}/accounts/${a.account_id}`} className="hover:text-ink">
                    {a.name || a.external_id} <span aria-hidden className="text-muted">→</span>
                  </Link>
                </td>
                <td className="px-4 py-2 text-right text-muted">{a.broker}</td>
                <td className="px-4 py-2 text-right text-muted">{a.currency}</td>
              </tr>
            ))}
          </Table>
        )}
      </Section>
    </div>
  );
}
