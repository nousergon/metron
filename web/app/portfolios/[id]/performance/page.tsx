import { acctParams, getPerformance, getSummary, MetronApiError } from "@/lib/api";
import { isoDate, moneyWhole, percent, signClass, signedMoneyWhole } from "@/lib/format";
import { Empty, Section, StatCard, Table } from "@/components/ui";
import { PortfolioNav } from "@/components/portfolio-nav";
import { BuildHistory } from "@/components/build-history";
import { NavChart } from "@/components/nav-chart";
import { AsOfClose } from "@/components/as-of-close";
import { NavBridge } from "@/components/nav-bridge";
import { RiskOverTime } from "@/components/risk-over-time";
import { TierSimulator } from "@/components/tier-simulator";
import { loadEntitlements, toFeatureStates } from "@/lib/entitlements";
import { requireApiAuth } from "@/lib/session";
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
  const apiAuth = await requireApiAuth();

  // URL selection wins; with none, the saved panel selection is applied (redirect).
  const accountIds = await resolveAccountIds(apiAuth, id, `/portfolios/${id}/performance`, searchParams.account_id);
  const navQuery = acctParams(accountIds);

  // Performance is now account-scoped (metron-ops#9): with a selection, the series comes
  // from those accounts' own forward-recorded NAV snapshots. The summary stays scoped to
  // the same selection so its Latest-NAV tile matches the series.
  const scoped = accountIds.length > 0;
  let perf, summary;
  try {
    [perf, summary] = await Promise.all([
      getPerformance(apiAuth, id, accountIds),
      getSummary(apiAuth, id, accountIds),
    ]);
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
  const entitlements = await loadEntitlements(apiAuth);

  // NAV bridge: latest NAV = value when history began + net contributions + the residual
  // investment gain (metron-ops#60).
  const bridge =
    perf.points.length >= 2 && perf.latest_nav != null
      ? (() => {
          const start = perf.points[0]!.nav;
          const contributions = perf.net_contributions;
          const end = perf.latest_nav!;
          return { start, contributions, gain: end - start - contributions, end };
        })()
      : null;

  // Per-calendar-year NAV bridge (metron-ops#68): for each year, start = the prior
  // snapshot's NAV (continuity), end = the last snapshot in the year, contributions = that
  // year's net external flow (the overall-first point carries no flow), gain = the residual.
  const yearBridges = (() => {
    type YB = { year: number; start: number; contributions: number; gain: number; end: number };
    if (perf.points.length < 2) return [] as YB[];
    const years: number[] = [];
    const idxByYear = new Map<number, number[]>();
    perf.points.forEach((p, i) => {
      const y = Number(p.snap_date.slice(0, 4));
      if (!idxByYear.has(y)) {
        idxByYear.set(y, []);
        years.push(y);
      }
      idxByYear.get(y)!.push(i);
    });
    return years.map((y): YB => {
      const idxs = idxByYear.get(y)!;
      const firstIdx = idxs[0]!;
      const start = firstIdx > 0 ? perf.points[firstIdx - 1]!.nav : perf.points[firstIdx]!.nav;
      const end = perf.points[idxs[idxs.length - 1]!]!.nav;
      const contributions = idxs.reduce((s, i) => s + (i > 0 ? perf.points[i]!.external_flow : 0), 0);
      return { year: y, start, contributions, gain: end - start - contributions, end };
    });
  })();

  return (
    <div>
      <PortfolioNav portfolioId={id} navQuery={navQuery} featureStates={toFeatureStates(entitlements)} />

      {entitlements ? <TierSimulator entitlements={entitlements} /> : null}

      <div className="mt-3 flex items-baseline gap-2">
        <h1 className="text-lg font-semibold">Performance</h1>
        {/* SETTLED tab (metron-ops#145/#146): every figure here is from the recorded
            EOD-close NAV history — the live intraday label belongs to Overview/Holdings only. */}
        <AsOfClose date={perf.last_date} />
      </div>
      <p className="text-sm text-muted">
        NAV records forward each time you refresh prices. To get instant history, build it from past prices.
      </p>

      {perf.estimated ? (
        <p className="mt-2 rounded border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-200">
          ⚠ {perf.estimated_note ?? "NAV history is estimated — some holdings lack complete lot data."} Returns
          and drawdown use the best available history; the figures firm up as lot/price data fills in.
        </p>
      ) : null}

      {scoped ? (
        <p className="mt-2 rounded border border-line bg-surface px-3 py-2 text-xs text-muted">
          ⓘ Scoped to your account selection. Per-account NAV can&apos;t be reconstructed from past prices
          (snapshot-sourced accounts like IBKR / SnapTrade report only current positions), so this series
          accrues forward from the first day each selected account was recorded — it&apos;ll be short at first
          and fill in daily. Clear the selection for the full reconstructable portfolio history.
        </p>
      ) : null}

      {/* Reconstruction is whole-portfolio only (it can't rebuild per-account NAV), so
          hide it when scoped — the note above tells the user to clear the selection. */}
      {scoped ? null : (
        <div className="mt-3">
          <BuildHistory portfolioId={id} feedOn={entitlements?.feed_enabled} />
        </div>
      )}

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
          <StatCard
            label="PSR"
            value={perf.psr != null ? `${(perf.psr * 100).toFixed(0)}%` : "—"}
            hint="P(true Sharpe > 0)"
          />
          <StatCard
            label="CVaR (95%)"
            value={perf.cvar != null ? percent(perf.cvar) : "—"}
            valueClass={signClass(perf.cvar ?? 0)}
            hint="avg. loss, worst 5% of periods"
          />
        </div>
      ) : null}

      {perf.rolling.length >= 2 ? <RiskOverTime rolling={perf.rolling} /> : null}

      {bridge ? (
        <div className="mt-4">
          <NavBridge
            start={bridge.start}
            contributions={bridge.contributions}
            gain={bridge.gain}
            end={bridge.end}
            currency={ccy}
            years={yearBridges}
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
                  {p.spy_close != null ? moneyWhole(p.spy_close, ccy) : "—"}
                </td>
              </tr>
            ))}
          </Table>
        )}
      </Section>
    </div>
  );
}
