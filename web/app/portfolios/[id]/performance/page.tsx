import Link from "next/link";
import { getPerformance, getSummary, MetronApiError } from "@/lib/api";
import { isoDate, money, percent, signClass, signedMoney } from "@/lib/format";
import { Empty, Section, StatCard, Table } from "@/components/ui";
import { BuildHistory } from "@/components/build-history";
import { requireTenantId } from "@/lib/session";

export const dynamic = "force-dynamic";

export default async function PerformancePage({ params }: { params: { id: string } }) {
  const { id } = params;
  const tenantId = await requireTenantId();

  let perf, summary;
  try {
    [perf, summary] = await Promise.all([getPerformance(tenantId, id), getSummary(tenantId, id)]);
  } catch (e) {
    if (e instanceof MetronApiError && e.status === 404) {
      return <Empty>Portfolio not found.</Empty>;
    }
    return <Empty>Couldn&apos;t load performance. Is the backend running?</Empty>;
  }

  const ccy = summary.base_currency;
  const hasMetrics = perf.twr != null || perf.cumulative_return != null;
  const recent = [...perf.points].reverse().slice(0, 30); // newest first

  return (
    <div>
      <Link href={`/portfolios/${id}`} className="text-sm text-muted hover:text-ink">
        ← Portfolio
      </Link>

      <h1 className="mt-3 text-lg font-semibold">Performance</h1>
      <p className="text-sm text-muted">
        NAV records forward each time you refresh prices. To get instant history, build it from past prices.
      </p>

      <div className="mt-3">
        <BuildHistory portfolioId={id} />
      </div>

      {hasMetrics ? (
        <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
          <StatCard
            label="Time-weighted return"
            value={perf.twr != null ? percent(perf.twr) : "—"}
            valueClass={signClass(perf.twr ?? 0)}
            hint={`${perf.days} days`}
          />
          <StatCard
            label="Annualized (TWR)"
            value={perf.annualized_twr != null ? percent(perf.annualized_twr) : "—"}
            valueClass={signClass(perf.annualized_twr ?? 0)}
          />
          <StatCard
            label="Cumulative"
            value={perf.cumulative_return != null ? percent(perf.cumulative_return) : "—"}
            valueClass={signClass(perf.cumulative_return ?? 0)}
            hint="net of contributions"
          />
          <StatCard
            label="Latest NAV"
            value={perf.latest_nav != null ? money(perf.latest_nav, ccy) : "—"}
            hint={perf.last_date ? isoDate(perf.last_date) : undefined}
          />
        </div>
      ) : (
        <div className="mt-4">
          <Empty>
            {perf.n_snapshots === 0
              ? "No NAV recorded yet. Refresh prices on the portfolio page to record the first day."
              : "One day recorded — refresh again on a later day and return metrics will appear."}
          </Empty>
        </div>
      )}

      {perf.alpha != null || perf.volatility != null ? (
        <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
          <StatCard
            label="Alpha vs SPY"
            value={perf.alpha != null ? percent(perf.alpha) : "—"}
            valueClass={signClass(perf.alpha ?? 0)}
            hint={perf.spy_return != null ? `SPY ${percent(perf.spy_return)}` : "TWR − SPY"}
          />
          <StatCard
            label="Volatility (ann.)"
            value={perf.volatility != null ? `${(perf.volatility * 100).toFixed(1)}%` : "—"}
          />
          <StatCard
            label="Sharpe"
            value={perf.sharpe != null ? perf.sharpe.toFixed(2) : "—"}
            valueClass={signClass(perf.sharpe ?? 0)}
            hint={perf.sortino != null ? `Sortino ${perf.sortino.toFixed(2)}` : undefined}
          />
          <StatCard
            label="Max drawdown"
            value={perf.max_drawdown != null ? percent(perf.max_drawdown) : "—"}
            valueClass={signClass(perf.max_drawdown ?? 0)}
          />
        </div>
      ) : null}

      <Section title="Recorded NAV" note={`${perf.n_snapshots} day${perf.n_snapshots === 1 ? "" : "s"}`}>
        {recent.length === 0 ? (
          <Empty>Nothing recorded yet.</Empty>
        ) : (
          <Table head={["Date", "NAV", "Net flow", "SPY close"]}>
            {recent.map((p) => (
              <tr key={p.snap_date} className="border-b border-line last:border-0">
                <td className="px-4 py-2 font-medium tabular-nums">{isoDate(p.snap_date)}</td>
                <td className="px-4 py-2 text-right tabular-nums">{money(p.nav, ccy)}</td>
                <td className={`px-4 py-2 text-right tabular-nums ${signClass(p.external_flow)}`}>
                  {p.external_flow ? signedMoney(p.external_flow, ccy) : "—"}
                </td>
                <td className="px-4 py-2 text-right tabular-nums text-muted">
                  {p.spy_close != null ? money(p.spy_close, ccy) : "—"}
                </td>
              </tr>
            ))}
          </Table>
        )}
      </Section>
    </div>
  );
}
