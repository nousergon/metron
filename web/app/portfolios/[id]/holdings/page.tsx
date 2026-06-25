import { acctParams, getAccounts, getHoldings, getHoldingsPerformanceSeries, getIntradayLegs, getSummary, getToday, MetronApiError, type HoldingsPerfSeries, type IntradayLegHistory, type Today } from "@/lib/api";
import { Empty, Section, StatCard } from "@/components/ui";
import { accountingMoneyWhole, percent, signClass } from "@/lib/format";
import { AccountPanel } from "@/components/account-panel";
import { AllocationBreakdown } from "@/components/allocation-breakdown";
import { GroupedHoldings } from "@/components/grouped-holdings";
import { HoldingsPerfChart } from "@/components/holdings-perf-chart";
import { TopBottomPerformers } from "@/components/top-bottom-performers";
import { RefreshPrices } from "@/components/refresh-prices";
import { IntradayRefresher } from "@/components/intraday-refresher";
import { PortfolioNav } from "@/components/portfolio-nav";
import { loadEntitlements, navFeatureStates } from "@/lib/entitlements";
import { requireTenantId } from "@/lib/session";
import { resolveAccountIds } from "@/lib/selection";

export const dynamic = "force-dynamic";

// Holdings — the position-level detail, separated from the Overview dashboard
// (metron-ops#64). Accounts are (de)activated HERE to see the effect on specific
// holdings; the selection persists and the Overview's aggregate metrics follow it.
export default async function HoldingsPage({
  params,
  searchParams,
}: {
  params: { id: string };
  searchParams: { account_id?: string | string[] };
}) {
  const { id } = params;
  const tenantId = await requireTenantId();
  const featureStates = await navFeatureStates(tenantId);

  const accountIds = await resolveAccountIds(tenantId, id, `/portfolios/${id}/holdings`, searchParams.account_id);
  const scoped = accountIds.length > 0;
  const navQuery = acctParams(accountIds);

  let summary, holdings, accounts;
  try {
    [summary, holdings, accounts] = await Promise.all([
      getSummary(tenantId, id, accountIds),
      getHoldings(tenantId, id, accountIds),
      getAccounts(tenantId, id),
    ]);
  } catch (e) {
    if (e instanceof MetronApiError && e.status === 404) {
      return <Empty>Portfolio not found.</Empty>;
    }
    return <Empty>Couldn&apos;t load holdings. Is the backend running?</Empty>;
  }

  const ccy = summary.base_currency;
  const priced = summary.market_value != null;
  const entitlements = await loadEntitlements(tenantId);

  // Per-account performance lines above the table (metron-ops#78) — best-effort, scoped to
  // the active account selection. Benchmark overlays are feed-gated server-side. Shown once
  // at least one account has ≥2 recorded NAV snapshots (a line needs two points).
  let perfSeries: HoldingsPerfSeries | null = null;
  try {
    perfSeries = await getHoldingsPerformanceSeries(tenantId, id, accountIds);
  } catch {
    perfSeries = null;
  }
  const showChart = (perfSeries?.accounts.length ?? 0) > 0;

  // Today's overnight/intraday/day P&L strip — folded in from the old Today page
  // (metron-ops#87). Best-effort: never blocks the page; hidden when there's no intraday
  // data (off-hours / no feed). The per-holding decomposition lives in the table's Day column.
  let today: Today | null = null;
  try {
    today = await getToday(tenantId, id, accountIds);
  } catch {
    today = null;
  }
  const showToday = !!today?.available && today.rows.length > 0;

  // Overnight-vs-intraday HISTORY (metron-ops#87) — the cumulative split of where the
  // portfolio's drift comes from. Best-effort; portfolio-wide (not account-scoped) and
  // empty until the daily recorder has ≥1 day.
  let legs: IntradayLegHistory | null = null;
  try {
    legs = await getIntradayLegs(tenantId, id);
  } catch {
    legs = null;
  }
  const showLegs = (legs?.n_days ?? 0) > 0 && (legs?.cum_day_pct != null);

  return (
    <div>
      <PortfolioNav portfolioId={id} navQuery={navQuery} featureStates={featureStates} />

      <div className="mt-3 flex items-baseline gap-2">
        <h1 className="text-lg font-semibold">Holdings</h1>
        {/* Position values revalue from intraday balances every ~5 min while open (#79). */}
        <IntradayRefresher portfolioId={id} />
      </div>
      <p className="text-sm text-muted">
        (De)activate accounts to see how they affect the positions below. The selection persists and the Overview
        metrics follow it.
      </p>

      {/* Today's P&L (folded in from the old Today page, metron-ops#87): Day = Overnight
          (open vs prior close) + Intraday (latest vs open). Per-holding detail is the Day
          column in the table below. */}
      {showToday && today ? (
        <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-3">
          <StatCard
            label="Overnight"
            value={today.overnight_gain != null ? accountingMoneyWhole(today.overnight_gain, ccy) : "—"}
            valueClass={signClass(today.overnight_gain ?? 0)}
            hint={today.overnight_pct != null ? percent(today.overnight_pct) : undefined}
          />
          <StatCard
            label="Intraday"
            value={today.intraday_gain != null ? accountingMoneyWhole(today.intraday_gain, ccy) : "—"}
            valueClass={signClass(today.intraday_gain ?? 0)}
            hint={today.intraday_pct != null ? percent(today.intraday_pct) : undefined}
          />
          <StatCard
            label="Day"
            value={today.day_gain != null ? accountingMoneyWhole(today.day_gain, ccy) : "—"}
            valueClass={signClass(today.day_gain ?? 0)}
            hint={today.day_pct != null ? percent(today.day_pct) : undefined}
          />
        </div>
      ) : null}

      {/* Overnight vs intraday HISTORY (metron-ops#87): the cumulative compounded split
          since recording began — where the portfolio's drift actually comes from. */}
      {showLegs && legs ? (
        <p className="mt-2 text-xs text-muted">
          Since tracking ({legs.n_days} day{legs.n_days === 1 ? "" : "s"}), cumulative drift split:{" "}
          <span className={signClass(legs.cum_overnight_pct ?? 0)}>
            overnight {legs.cum_overnight_pct != null ? percent(legs.cum_overnight_pct) : "—"}
          </span>{" "}
          ·{" "}
          <span className={signClass(legs.cum_intraday_pct ?? 0)}>
            intraday {legs.cum_intraday_pct != null ? percent(legs.cum_intraday_pct) : "—"}
          </span>{" "}
          ·{" "}
          <span className={signClass(legs.cum_day_pct ?? 0)}>
            day {legs.cum_day_pct != null ? percent(legs.cum_day_pct) : "—"}
          </span>
        </p>
      ) : null}

      <Section title="Accounts">
        <AccountPanel accounts={accounts} baseCurrency={ccy} portfolioId={id} />
        {scoped ? (
          <p className="mt-2 text-xs text-muted">
            Showing {summary.n_accounts} of {accounts.length} account{accounts.length === 1 ? "" : "s"} — the holdings
            below reflect this selection.
          </p>
        ) : null}
      </Section>

      {/* Per-account performance lines above the table (metron-ops#78). */}
      {showChart && perfSeries ? (
        <Section title="Performance">
          <HoldingsPerfChart
            accounts={perfSeries.accounts}
            benchmarks={perfSeries.benchmarks}
            benchmarksAvailable={perfSeries.benchmarks_available}
          />
        </Section>
      ) : null}

      {/* Best/worst performers + the country/sector allocation, from the holdings already
          loaded above. Performers needs priced returns; allocation needs market value. */}
      {holdings.length > 0 && priced ? (
        <Section title="Performers" note="best & worst holdings by return">
          <TopBottomPerformers holdings={holdings} />
        </Section>
      ) : null}

      {holdings.length > 0 ? (
        <Section title="Allocation" note="by market value">
          <AllocationBreakdown holdings={holdings} baseCurrency={ccy} />
        </Section>
      ) : null}

      <Section title="Holdings" note={priced ? `all values in ${ccy} · market value from last EOD close` : "cost basis — refresh for market value"}>
        <div className="mb-3">
          <RefreshPrices portfolioId={id} feedOn={entitlements?.feed_enabled} />
        </div>
        {holdings.length === 0 ? (
          <Empty>No open positions.</Empty>
        ) : (
          <GroupedHoldings holdings={holdings} baseCurrency={ccy} priced={priced} portfolioId={id} />
        )}
      </Section>
    </div>
  );
}
