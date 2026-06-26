import { Suspense } from "react";
import { acctParams, getAccounts, getHoldings, getHoldingsPerformanceSeries, getIntradayLegs, getSummary, getToday, getValuationMedians, MetronApiError, type Entitlements, type Holding, type HoldingsPerfSeries, type IntradayLegHistory, type Summary, type Today, type ValuationMedians } from "@/lib/api";
import { Empty, Section, StatCard } from "@/components/ui";
import { accountingMoneyWhole, percent, signClass } from "@/lib/format";
import { AccountPanel } from "@/components/account-panel";
import { AllocationBreakdown } from "@/components/allocation-breakdown";
import { HoldingsView } from "@/components/holdings-view";
import { HoldingsPerfChart } from "@/components/holdings-perf-chart";
import { TopBottomPerformers } from "@/components/top-bottom-performers";
import { RefreshPrices } from "@/components/refresh-prices";
import { IntradayRefresher } from "@/components/intraday-refresher";
import { PortfolioNav } from "@/components/portfolio-nav";
import { loadEntitlements, navFeatureStates } from "@/lib/entitlements";
import { requireTenantId } from "@/lib/session";
import { resolveAccountIds } from "@/lib/selection";

export const dynamic = "force-dynamic";

/** A pulsing placeholder bar (mirrors loading.tsx). */
function Bar({ className = "" }: { className?: string }) {
  return <div className={`rounded bg-line/60 ${className}`} />;
}

/** Skeleton fallback for a streamed Section while its data loads. */
function SectionSkeleton({ rows = 3 }: { rows?: number }) {
  return (
    <section className="mt-8 animate-pulse" aria-busy="true">
      <Bar className="h-3 w-32" />
      <div className="mt-3 space-y-2">
        {Array.from({ length: rows }).map((_, i) => (
          <Bar key={i} className="h-10 w-full" />
        ))}
      </div>
    </section>
  );
}

// Holdings — the position-level detail, separated from the Overview dashboard
// (metron-ops#64). Accounts are (de)activated HERE to see the effect on specific
// holdings; the selection persists and the Overview's aggregate metrics follow it.
//
// Streaming layout (perf): the shell + the fast Today strip paint immediately (Phase A +
// the two cheap reads, summary/today), then the three expensive sections (Accounts,
// Performance chart, Holdings table) each stream in behind their own <Suspense> as their
// data lands, instead of the whole page blocking on the slowest fetch. Each streamed
// section is an async Server Component that fetches only its own slice and fails soft.
export default async function HoldingsPage({
  params,
  searchParams,
}: {
  params: { id: string };
  searchParams: { account_id?: string | string[] };
}) {
  const { id } = params;
  const tenantId = await requireTenantId();

  // Phase A — everything independent of the resolved account selection, in parallel.
  const [featureStates, entitlements, legs, accountIds] = await Promise.all([
    navFeatureStates(tenantId),
    loadEntitlements(tenantId),
    getIntradayLegs(tenantId, id).catch((): IntradayLegHistory | null => null),
    resolveAccountIds(tenantId, id, `/portfolios/${id}/holdings`, searchParams.account_id),
  ]);
  const scoped = accountIds.length > 0;
  const navQuery = acctParams(accountIds);

  // The two CHEAP scoped reads block the shell (≈0.3s): summary gives base currency +
  // priced state used throughout (and resolves portfolio-not-found), today drives the
  // P&L strip. The expensive reads (accounts / perf series / holdings) stream below.
  let summary: Summary;
  let today: Today | null = null;
  try {
    [summary, today] = await Promise.all([
      getSummary(tenantId, id, accountIds),
      getToday(tenantId, id, accountIds).catch((): Today | null => null),
    ]);
  } catch (e) {
    if (e instanceof MetronApiError && e.status === 404) {
      return <Empty>Portfolio not found.</Empty>;
    }
    return <Empty>Couldn&apos;t load holdings. Is the backend running?</Empty>;
  }

  const ccy = summary.base_currency;
  const priced = summary.market_value != null;

  const showToday = !!today?.available && today.rows.length > 0;
  const showLegs = (legs?.n_days ?? 0) > 0 && legs?.cum_day_pct != null;

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

      {/* Today's P&L (metron-ops#87): Day = Overnight (open vs prior close) + Intraday
          (latest vs open). Per-holding detail is the Day column in the table below. */}
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

      {/* Overnight vs intraday HISTORY (metron-ops#87): cumulative compounded split. */}
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

      <Suspense fallback={<SectionSkeleton rows={2} />}>
        <AccountsSection tenantId={tenantId} id={id} ccy={ccy} scoped={scoped} nSelected={summary.n_accounts} />
      </Suspense>

      <Suspense fallback={<SectionSkeleton rows={4} />}>
        <PerfChartSection tenantId={tenantId} id={id} accountIds={accountIds} />
      </Suspense>

      <Suspense fallback={<SectionSkeleton rows={6} />}>
        <HoldingsSection tenantId={tenantId} id={id} accountIds={accountIds} ccy={ccy} priced={priced} entitlements={entitlements} />
      </Suspense>
    </div>
  );
}

// --- streamed sections -----------------------------------------------------

async function AccountsSection({
  tenantId, id, ccy, scoped, nSelected,
}: {
  tenantId: string; id: string; ccy: string; scoped: boolean; nSelected: number;
}) {
  const accounts = await getAccounts(tenantId, id).catch(() => null);
  if (!accounts) return null;
  return (
    <Section title="Accounts">
      <AccountPanel accounts={accounts} baseCurrency={ccy} portfolioId={id} />
      {scoped ? (
        <p className="mt-2 text-xs text-muted">
          Showing {nSelected} of {accounts.length} account{accounts.length === 1 ? "" : "s"} — the holdings below
          reflect this selection.
        </p>
      ) : null}
    </Section>
  );
}

async function PerfChartSection({
  tenantId, id, accountIds,
}: {
  tenantId: string; id: string; accountIds: string[];
}) {
  const perfSeries = await getHoldingsPerformanceSeries(tenantId, id, accountIds).catch(
    (): HoldingsPerfSeries | null => null,
  );
  // Per-account performance lines (metron-ops#78) — shown once ≥1 account has ≥2 NAV points.
  if (!perfSeries || perfSeries.accounts.length === 0) return null;
  return (
    <Section title="Performance">
      <HoldingsPerfChart
        accounts={perfSeries.accounts}
        benchmarks={perfSeries.benchmarks}
        benchmarksAvailable={perfSeries.benchmarks_available}
      />
    </Section>
  );
}

async function HoldingsSection({
  tenantId, id, accountIds, ccy, priced, entitlements,
}: {
  tenantId: string; id: string; accountIds: string[]; ccy: string; priced: boolean; entitlements: Entitlements | null;
}) {
  let holdings: Holding[];
  try {
    holdings = await getHoldings(tenantId, id, accountIds);
  } catch {
    return (
      <Section title="Holdings">
        <Empty>Couldn&apos;t load holdings.</Empty>
      </Section>
    );
  }
  // SP1500-broad sector/country median bands — best-effort + feed-gated.
  const medians = await getValuationMedians(tenantId, id, accountIds).catch((): ValuationMedians | null => null);

  return (
    <>
      {/* Best/worst performers + country/sector allocation, from the holdings loaded here. */}
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
          <HoldingsView holdings={holdings} baseCurrency={ccy} priced={priced} medians={medians} portfolioId={id} />
        )}
      </Section>
    </>
  );
}
