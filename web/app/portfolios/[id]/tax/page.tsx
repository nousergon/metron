import Link from "next/link";
import { getSummary, getTax, MetronApiError } from "@/lib/api";
import { isoDate, money, quantity, signClass, signedMoney } from "@/lib/format";
import { Empty, Section, StatCard, Table } from "@/components/ui";
import { requireTenantId } from "@/lib/session";

export const dynamic = "force-dynamic";

export default async function TaxPage({ params }: { params: { id: string } }) {
  const { id } = params;
  const tenantId = await requireTenantId();

  let taxData, summary;
  try {
    [taxData, summary] = await Promise.all([getTax(tenantId, id), getSummary(tenantId, id)]);
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
      <Link href={`/portfolios/${id}`} className="text-sm text-muted hover:text-ink">
        ← Portfolio
      </Link>

      <h1 className="mt-3 text-lg font-semibold">Tax</h1>
      <p className="text-sm text-muted">
        Per-lot holding-period term and unrealized P&amp;L (at the last close), with harvestable losses flagged.
        Descriptive, not advice.
      </p>

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
            value={money(taxData.harvestable_loss, ccy)}
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
          <Table head={["Ticker", "Opened", "Term", "Quantity", "Cost basis", "Market value", "Unrealized", "Harvest"]}>
            {taxData.lots.map((l, i) => (
              <tr key={`${l.ticker}-${l.open_date}-${i}`} className="border-b border-line last:border-0">
                <td className="px-4 py-2 font-medium">{l.ticker}</td>
                <td className="px-4 py-2 text-right text-muted">{isoDate(l.open_date)}</td>
                <td className="px-4 py-2 text-right text-muted">{l.term === "Long-term" ? "LT" : l.term === "Short-term" ? "ST" : "?"}</td>
                <td className="px-4 py-2 text-right tabular-nums">{quantity(l.quantity)}</td>
                <td className="px-4 py-2 text-right tabular-nums">{money(l.cost_basis, ccy)}</td>
                <td className="px-4 py-2 text-right tabular-nums">{l.market_value != null ? money(l.market_value, ccy) : "—"}</td>
                <td className={`px-4 py-2 text-right tabular-nums ${signClass(l.unrealized_gain ?? 0)}`}>
                  {l.unrealized_gain != null ? signedMoney(l.unrealized_gain, ccy) : "—"}
                </td>
                <td className="px-4 py-2 text-right tabular-nums">
                  {l.harvestable_loss > 0 ? <span className="text-negative">{money(l.harvestable_loss, ccy)}</span> : "—"}
                </td>
              </tr>
            ))}
          </Table>
        )}
      </Section>
    </div>
  );
}
