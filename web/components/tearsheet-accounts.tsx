import { money, moneyWhole, percent, quantity, signClass, signedMoneyWhole } from "@/lib/format";
import { Section, Table } from "@/components/ui";
import type { TearsheetAccountPosition } from "@/lib/api";

/** Per-account breakdown of one holding on the tearsheet: which accounts hold it, how much,
 *  and each account's own return (cost basis is per account, so returns differ by lot). */
export function TearsheetAccounts({
  legs,
  baseCurrency,
  nativeCurrency,
}: {
  legs: TearsheetAccountPosition[];
  baseCurrency: string;
  nativeCurrency: string;
}) {
  if (legs.length === 0) return null;
  const dash = "—";
  return (
    <Section title="By account" note={`${legs.length} account${legs.length === 1 ? "" : "s"} · values in ${baseCurrency}`}>
      <Table head={["Account", "Shares", "Avg cost", "Market value", "Unrealized", "Return", "Share"]}>
        {legs.map((a) => (
          <tr key={a.account_id} className="border-b border-line last:border-0">
            <td className="px-4 py-2">
              {a.account_label}
              {a.account_type ? <span className="ml-2 text-xs text-muted">{a.account_type}</span> : null}
            </td>
            <td className="px-4 py-2 text-right tabular-nums">{quantity(a.quantity)}</td>
            <td className="px-4 py-2 text-right tabular-nums">{money(a.avg_cost, nativeCurrency)}</td>
            <td className="px-4 py-2 text-right tabular-nums">
              {a.market_value != null ? moneyWhole(a.market_value, baseCurrency) : dash}
            </td>
            <td className={`px-4 py-2 text-right tabular-nums ${signClass(a.unrealized_gain ?? 0)}`}>
              {a.unrealized_gain != null ? signedMoneyWhole(a.unrealized_gain, baseCurrency) : dash}
            </td>
            <td className={`px-4 py-2 text-right tabular-nums ${signClass(a.unrealized_pct ?? 0)}`}>
              {a.unrealized_pct != null ? percent(a.unrealized_pct) : dash}
            </td>
            <td className="px-4 py-2 text-right tabular-nums">{a.share_pct != null ? percent(a.share_pct) : dash}</td>
          </tr>
        ))}
      </Table>
    </Section>
  );
}
