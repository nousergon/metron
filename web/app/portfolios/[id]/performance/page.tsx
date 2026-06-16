import { acctParams, getPerformance, getSummary, MetronApiError } from "@/lib/api";
import { isoDate, money, moneyWhole, percent, signClass, signedMoneyWhole } from "@/lib/format";
import { Empty, Section, StatCard, Table } from "@/components/ui";
import { PortfolioNav } from "@/components/portfolio-nav";
import { BuildHistory } from "@/components/build-history";
import { NavChart } from "@/components/nav-chart";
import { TierSimulator } from "@/components/tier-simulator";
import { loadEntitlements } from "@/lib/entitlements";
import { requireTenantId } from "@/lib/session";
import { resolveAccountIds } from "@/lib/selection";

export const dynamic = "force-dynamic";

export default async function PerformancePage({
  params,
  searchParams,
}: {
  params: { id: string };
  searchParams: { account_id?: string | string[] };
}) {
  const { id } = params;
  const tenantId = await requireTenantId();

  // URL selection wins; with none, the saved panel selection is applied (redirect).
  const accountIds = await resolveAccountIds(tenantId, id, `/portfolios/${id}/performance`, searchParams.account_id);
  const navQuery = acctParams(accountIds);

  // Performance is whole-portfolio — per-account NAV history can't be reconstructed for
  // snapshot-sourced accounts, so the series + summary stay unscoped (note shown below).
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
  // perf.points are ascending (oldest → newest) — the chart wants chronological order.
  const navSeries = perf.points.map((p) => ({ snap_date: p.snap_date, nav: p.nav }));
  const entitlements = await loadEntitlements(tenantId);

  return (
    <div>
      <PortfolioNav portfolioId={id} navQuery={navQuery} />

      {entitlements ? <TierSimulator entitlements={entitlements} /> : null}

      <h1 className="mt-3 text-lg font-semibold">Performance</h1>
      <p className="text-sm text-muted">
        NAV records forward each time you refresh prices. To get instant history, build it from past prices.
      </p>

      {accountIds.length > 0 ? (
        <p className="mt-2 rounded border border-line bg-surface px-3 py-2 text-xs text-muted">
          ⓘ Performance reflects the whole portfolio. Per-account history is still accruing — snapshot-sourced
          accounts (IBKR / SnapTrade) have no back-history to reconstruct, so a per-account NAV-vs-SPY series
          builds forward from today.
        </p>
      ) : null}

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
            value={perf.latest_nav != null ? moneyWhole(perf.latest_nav, ccy) : "—"}
            hint={perf.last_date ? isoDate(perf.last_date) : undefined}
          />
        </div>
      ) : (
        <div className="mt-4">
          <Empty>
            {perf.n_snapshots === 0
              ? "No NAV recorded yet. The nightly refresh seeds history automatically — or click “Build history” now to backfill it from past prices."
              : "One day recorded — the nightly refresh adds more, or “Build history” backfills the full series now."}
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

      {navSeries.length >= 2 ? (
        <div className="mt-4">
          <NavChart points={navSeries} currency={ccy} />
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
                <td className="px-4 py-2 text-right tabular-nums">{moneyWhole(p.nav, ccy)}</td>
                <td className={`px-4 py-2 text-right tabular-nums ${signClass(p.external_flow)}`}>
                  {p.external_flow ? signedMoneyWhole(p.external_flow, ccy) : "—"}
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
