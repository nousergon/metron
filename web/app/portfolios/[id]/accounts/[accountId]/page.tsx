import Link from "next/link";
import { getAccountDetail, MetronApiError } from "@/lib/api";
import { isoDate, money, quantity, signClass, signedMoney } from "@/lib/format";
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

      <Section title="Holdings" note="cost basis (market value pending a price feed)">
        {holdings.length === 0 ? (
          <Empty>No open positions in this account.</Empty>
        ) : (
          <Table head={["Ticker", "Quantity", "Avg cost", "Cost basis"]}>
            {holdings.map((h) => (
              <tr key={h.ticker} className="border-b border-line last:border-0">
                <td className="px-4 py-2 font-medium">{h.ticker}</td>
                <td className="px-4 py-2 text-right tabular-nums">{quantity(h.quantity)}</td>
                <td className="px-4 py-2 text-right tabular-nums">{money(h.avg_cost, ccy)}</td>
                <td className="px-4 py-2 text-right tabular-nums">{money(h.cost_basis, ccy)}</td>
              </tr>
            ))}
          </Table>
        )}
      </Section>

      <Section title="Realized lots" note="closed positions — FIFO, short/long term">
        {lots.length === 0 ? (
          <Empty>No closed lots in this account.</Empty>
        ) : (
          <Table head={["Ticker", "Opened", "Closed", "Quantity", "Proceeds", "Cost basis", "Gain", "Term"]}>
            {lots.map((r, i) => (
              <tr key={`${r.ticker}-${r.close_date}-${i}`} className="border-b border-line last:border-0">
                <td className="px-4 py-2 font-medium">{r.ticker}</td>
                <td className="px-4 py-2 text-right text-muted">{isoDate(r.open_date)}</td>
                <td className="px-4 py-2 text-right text-muted">{isoDate(r.close_date)}</td>
                <td className="px-4 py-2 text-right tabular-nums">{quantity(r.quantity)}</td>
                <td className="px-4 py-2 text-right tabular-nums">{money(r.proceeds, ccy)}</td>
                <td className="px-4 py-2 text-right tabular-nums">{money(r.cost_basis, ccy)}</td>
                <td className={`px-4 py-2 text-right font-medium tabular-nums ${signClass(r.gain)}`}>
                  {signedMoney(r.gain, ccy)}
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
          <Table head={["Date", "Type", "Ticker", "Quantity", "Price", "Amount", "Fees"]}>
            {txns.map((t, i) => (
              <tr key={`${t.trade_date}-${t.txn_type}-${t.ticker}-${i}`} className="border-b border-line last:border-0">
                <td className="px-4 py-2 font-medium tabular-nums">{isoDate(t.trade_date)}</td>
                <td className="px-4 py-2 text-right text-muted">{t.txn_type}</td>
                <td className="px-4 py-2 text-right">{t.ticker || "—"}</td>
                <td className="px-4 py-2 text-right tabular-nums">{t.quantity ? quantity(t.quantity) : "—"}</td>
                <td className="px-4 py-2 text-right tabular-nums">{t.price ? money(t.price, ccy) : "—"}</td>
                <td className="px-4 py-2 text-right tabular-nums">{money(t.amount, ccy)}</td>
                <td className="px-4 py-2 text-right tabular-nums text-muted">{t.fees ? money(t.fees, ccy) : "—"}</td>
              </tr>
            ))}
          </Table>
        )}
      </Section>
    </div>
  );
}
