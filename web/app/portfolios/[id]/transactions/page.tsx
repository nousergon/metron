import Link from "next/link";
import { getRealized, getSummary, getTransactions, MetronApiError } from "@/lib/api";
import { isoDate, money, quantity, signClass, signedMoney } from "@/lib/format";
import { Empty, Section, Table } from "@/components/ui";
import { requireTenantId } from "@/lib/session";

export const dynamic = "force-dynamic";

export default async function TransactionsPage({ params }: { params: { id: string } }) {
  const { id } = params;
  const tenantId = await requireTenantId();

  let summary, transactions, realized;
  try {
    [summary, transactions, realized] = await Promise.all([
      getSummary(tenantId, id),
      getTransactions(tenantId, id),
      getRealized(tenantId, id),
    ]);
  } catch (e) {
    if (e instanceof MetronApiError && e.status === 404) {
      return <Empty>Portfolio not found.</Empty>;
    }
    return <Empty>Couldn&apos;t load this portfolio. Is the backend running?</Empty>;
  }

  const ccy = summary.base_currency;
  // Backend returns both oldest-first; a history reads best newest-first.
  const txns = [...transactions].reverse();
  const lots = [...realized].reverse();

  return (
    <div>
      <Link href={`/portfolios/${id}`} className="text-sm text-muted hover:text-ink">
        ← Portfolio
      </Link>

      <h1 className="mt-3 text-lg font-semibold">Activity</h1>

      <Section title="Realized lots" note={`closed positions — FIFO; gain in ${ccy} at the close-date FX rate`}>
        {lots.length === 0 ? (
          <Empty>No closed lots yet.</Empty>
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

      <Section title="Transactions" note={`${txns.length} imported — newest first`}>
        {txns.length === 0 ? (
          <Empty>No transactions imported yet.</Empty>
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
