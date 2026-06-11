import Link from "next/link";
import { getAccountDetail, MetronApiError } from "@/lib/api";
import { isoDate, money, percent, quantity, signClass, signedMoney } from "@/lib/format";
import { Empty, Section, Table } from "@/components/ui";
import { requireTenantId } from "@/lib/session";

export const dynamic = "force-dynamic";

export default async function AccountPage({ params }: { params: { id: string; accountId: string } }) {
  const { id, accountId } = params;
  const tenantId = await requireTenantId();

  let detail;
  try {
    detail = await getAccountDetail(tenantId, id, accountId);
  } catch (e) {
    if (e instanceof MetronApiError && e.status === 404) {
      return <Empty>Account not found.</Empty>;
    }
    return <Empty>Couldn&apos;t load this account. Is the backend running?</Empty>;
  }

  const { account, holdings, realized, transactions } = detail;
  const ccy = account.currency;
  const priced = holdings.some((h) => h.market_value != null);
  // Backend returns both oldest-first; a history reads best newest-first.
  const txns = [...transactions].reverse();
  const lots = [...realized].reverse();

  return (
    <div>
      <Link href={`/portfolios/${id}`} className="text-sm text-muted hover:text-ink">
        ← Portfolio
      </Link>

      <h1 className="mt-3 text-lg font-semibold">{account.name || account.external_id}</h1>
      <p className="text-sm text-muted">
        {account.broker} · {account.external_id} · {account.currency}
      </p>

      <Section title="Holdings" note={priced ? "market value from last EOD close" : "cost basis (refresh prices on the portfolio)"}>
        {holdings.length === 0 ? (
          <Empty>No open positions in this account.</Empty>
        ) : (
          <Table
            head={
              priced
                ? ["Ticker", "Ccy", "Quantity", "Avg cost", "Cost basis", "Last", "Market value", "Unrealized"]
                : ["Ticker", "Ccy", "Quantity", "Avg cost", "Cost basis"]
            }
          >
            {holdings.map((h) => {
              const foreign = h.currency !== ccy;
              return (
                <tr key={h.ticker} className="border-b border-line last:border-0">
                  <td className="px-4 py-2 font-medium">{h.ticker}</td>
                  <td className="px-4 py-2 text-muted">{h.currency}</td>
                  <td className="px-4 py-2 text-right tabular-nums">{quantity(h.quantity)}</td>
                  <td className="px-4 py-2 text-right tabular-nums">{money(h.avg_cost, h.currency)}</td>
                  <td className="px-4 py-2 text-right tabular-nums">{money(h.cost_basis, h.currency)}</td>
                  {priced ? (
                    <>
                      <td className="px-4 py-2 text-right tabular-nums text-muted">
                        {h.last_price != null ? money(h.last_price, h.currency) : "—"}
                      </td>
                      <td className="px-4 py-2 text-right tabular-nums">
                        {h.market_value != null ? (
                          money(h.market_value, ccy)
                        ) : foreign && h.market_value_local != null ? (
                          <span className="text-muted" title={`No ${ccy} FX rate cached`}>
                            {money(h.market_value_local, h.currency)}*
                          </span>
                        ) : (
                          "—"
                        )}
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
              );
            })}
          </Table>
        )}
      </Section>

      <Section title="Realized lots" note={`closed positions — FIFO; gain in ${ccy} at the close-date FX rate`}>
        {lots.length === 0 ? (
          <Empty>No closed lots in this account.</Empty>
        ) : (
          <Table head={["Ticker", "Ccy", "Opened", "Closed", "Quantity", "Proceeds", "Cost basis", "Gain", "Term"]}>
            {lots.map((r, i) => (
              <tr key={`${r.ticker}-${r.close_date}-${i}`} className="border-b border-line last:border-0">
                <td className="px-4 py-2 font-medium">{r.ticker}</td>
                <td className="px-4 py-2 text-muted">{r.currency}</td>
                <td className="px-4 py-2 text-right text-muted">{isoDate(r.open_date)}</td>
                <td className="px-4 py-2 text-right text-muted">{isoDate(r.close_date)}</td>
                <td className="px-4 py-2 text-right tabular-nums">{quantity(r.quantity)}</td>
                <td className="px-4 py-2 text-right tabular-nums">{money(r.proceeds, r.currency)}</td>
                <td className="px-4 py-2 text-right tabular-nums">{money(r.cost_basis, r.currency)}</td>
                <td className={`px-4 py-2 text-right font-medium tabular-nums ${signClass(r.gain_base ?? r.gain)}`}>
                  {r.gain_base != null ? (
                    signedMoney(r.gain_base, ccy)
                  ) : (
                    <span className="text-muted" title={`No ${ccy} FX rate for ${isoDate(r.close_date)}`}>
                      {signedMoney(r.gain, r.currency)}*
                    </span>
                  )}
                </td>
                <td className="px-4 py-2 text-right text-muted">{r.long_term ? "Long" : "Short"}</td>
              </tr>
            ))}
          </Table>
        )}
      </Section>

      <Section title="Transactions" note={`${txns.length} in this account — newest first`}>
        {txns.length === 0 ? (
          <Empty>No transactions in this account.</Empty>
        ) : (
          <Table head={["Date", "Type", "Ticker", "Ccy", "Quantity", "Price", "Amount", "Fees"]}>
            {txns.map((t, i) => (
              <tr key={`${t.trade_date}-${t.txn_type}-${t.ticker}-${i}`} className="border-b border-line last:border-0">
                <td className="px-4 py-2 font-medium tabular-nums">{isoDate(t.trade_date)}</td>
                <td className="px-4 py-2 text-right text-muted">{t.txn_type}</td>
                <td className="px-4 py-2 text-right">{t.ticker || "—"}</td>
                <td className="px-4 py-2 text-right text-muted">{t.currency}</td>
                <td className="px-4 py-2 text-right tabular-nums">{t.quantity ? quantity(t.quantity) : "—"}</td>
                <td className="px-4 py-2 text-right tabular-nums">{t.price ? money(t.price, t.currency) : "—"}</td>
                <td className="px-4 py-2 text-right tabular-nums">{money(t.amount, t.currency)}</td>
                <td className="px-4 py-2 text-right tabular-nums text-muted">{t.fees ? money(t.fees, t.currency) : "—"}</td>
              </tr>
            ))}
          </Table>
        )}
      </Section>
    </div>
  );
}
