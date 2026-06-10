import Link from "next/link";
import { getMacro, MetronApiError, type MacroIndicator } from "@/lib/api";
import { isoDate, signClass } from "@/lib/format";
import { Empty, Section, Table } from "@/components/ui";
import { requireTenantId } from "@/lib/session";

export const dynamic = "force-dynamic";

function value(ind: MacroIndicator): string {
  return ind.units === "%" ? `${ind.latest_value.toFixed(2)}%` : ind.latest_value.toFixed(2);
}

function change(ind: MacroIndicator): string {
  if (ind.change == null) return "—";
  const sign = ind.change > 0 ? "+" : ind.change < 0 ? "−" : "";
  const mag = Math.abs(ind.change).toFixed(2);
  return ind.units === "%" ? `${sign}${mag} pp` : `${sign}${mag}`;
}

export default async function MacroPage({ params }: { params: { id: string } }) {
  const { id } = params;
  const tenantId = await requireTenantId();

  let macro;
  try {
    macro = await getMacro(tenantId);
  } catch (e) {
    if (e instanceof MetronApiError && e.status === 404) {
      return <Empty>Portfolio not found.</Empty>;
    }
    return <Empty>Couldn&apos;t load macro data. Is the backend running?</Empty>;
  }

  return (
    <div>
      <Link href={`/portfolios/${id}`} className="text-sm text-muted hover:text-ink">
        ← Portfolio
      </Link>

      <h1 className="mt-3 text-lg font-semibold">Macro</h1>
      <p className="text-sm text-muted">
        Rates, the yield curve, inflation expectations, and volatility — from FRED
        {macro.as_of ? `, as of ${isoDate(macro.as_of)}` : ""}.
      </p>

      {!macro.available ? (
        <div className="mt-4">
          <Empty>{macro.reason ?? "Macro data unavailable."}</Empty>
        </div>
      ) : (
        <Section title="Indicators" note="latest reading + change vs the prior reading">
          <Table head={["Indicator", "Latest", "Change", "As of"]}>
            {macro.indicators.map((ind) => (
              <tr key={ind.key} className="border-b border-line last:border-0">
                <td className="px-4 py-2 font-medium">{ind.label}</td>
                <td className="px-4 py-2 text-right tabular-nums">{value(ind)}</td>
                <td className={`px-4 py-2 text-right tabular-nums ${signClass(ind.change ?? 0)}`}>{change(ind)}</td>
                <td className="px-4 py-2 text-right tabular-nums text-muted">{isoDate(ind.latest_date)}</td>
              </tr>
            ))}
          </Table>
        </Section>
      )}
    </div>
  );
}
