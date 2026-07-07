import { Suspense } from "react";
import { getAccounts, getHoldings, getHoldingsPerformanceSeries, getIndices, getPerformanceTiles, getPlugins, getPortfolio, getSummary, MetronApiError, type Account, type Holding, type HoldingsPerfSeries, type PeriodTiles, type Portfolio, type PluginNav, type Summary } from "@/lib/api";
import { accountingMoneyWhole, moneyWhole, percent, signClass, signedMoneyWhole } from "@/lib/format";
import { Empty, Section, StatCard } from "@/components/ui";
import { AllocationBreakdown } from "@/components/allocation-breakdown";
import { HoldingsPerfChart } from "@/components/holdings-perf-chart";
import { TopBottomPerformers } from "@/components/top-bottom-performers";
import { PerfTiles } from "@/components/perf-tiles";
import { PortfolioNav } from "@/components/portfolio-nav";
import { TierSimulator } from "@/components/tier-simulator";
import { IndexStrip } from "@/components/index-strip";
import { SettledRefresher } from "@/components/settled-refresher";
import { RenamePortfolio } from "@/components/rename-portfolio";
import { featureEntitlement, loadEntitlements, previewFromCookies, toFeatureStates } from "@/lib/entitlements";
import { requireTenantId } from "@/lib/session";
import Link from "next/link";

export const dynamic = "force-dynamic";

/** Taxable per the 3-way treatment, falling back to the derived binary flag. */
function isTaxable(a: Account): boolean {
  if (a.tax_treatment === "taxable") return true;
  if (a.tax_treatment === "tax_deferred" || a.tax_treatment === "tax_exempt") return false;
  return a.taxable;
}

/** Sum a field over accounts, returning null only when no account carries it. */
function sumOrNull(accts: Account[], pick: (a: Account) => number | null): number | null {
  let total = 0;
  let any = false;
  for (const a of accts) {
    const v = pick(a);
    if (v != null) {
      total += v;
      any = true;
    }
  }
  return any ? total : null;
}

export default async function PortfolioPage({
  params,
}: {
  params: { id: string };
}) {
  const { id } = params;
  const tenantId = await requireTenantId();

  // The Overview is the WHOLE-PORTFOLIO dashboard — it never scopes to an account subset.
  // Account (de)selection is a Holdings concern, so every aggregate here (headline value,
  // performance chart, allocation, performers, today's P&L) anchors on ALL accounts. We
  // ignore any ?account_id= entirely (no checkboxes on this page) and the nav links carry
  // no scoping — Holdings owns its own selection.
  const accountIds: string[] = [];
  const scoped = false;
  const navQuery = "";

  // Streaming layout (perf, metron-ops#108): the headline + the whole-portfolio metrics
  // derive entirely from `summary` (≈0.25s), so the page paints them immediately while the
  // accounts/holdings-dependent regions stream behind <Suspense>. Each streamed region
  // fetches its own slice; #129's server-side single-flight cache dedupes the parallel
  // `getAccounts` reads into one upstream call (and serves it instantly on repeat visits).
  let portfolio: Portfolio, summary: Summary;
  try {
    [portfolio, summary] = await Promise.all([
      getPortfolio(tenantId, id),
      getSummary(tenantId, id, accountIds),
    ]);
  } catch (e) {
    if (e instanceof MetronApiError && e.status === 404) {
      return <Empty>Portfolio not found.</Empty>;
    }
    return <Empty>Couldn&apos;t load this portfolio. Is the backend running?</Empty>;
  }

  const ccy = summary.base_currency;
  const priced = summary.market_value != null;

  const realizedYtdTaxable = summary.realized_st_ytd + summary.realized_lt_ytd;

  // Premium nav (metron-ops). Best-effort + always empty on the public tier.
  let plugins: PluginNav[] = [];
  try {
    plugins = await getPlugins(tenantId);
  } catch {
    plugins = [];
  }

  const entitlements = await loadEntitlements(tenantId);
  const featureStates = toFeatureStates(entitlements);

  // Performance-vs-market hero tiles (metron-ops#83) — best-effort, whole-portfolio.
  // Benchmark columns are feed-gated server-side (portfolio-only in the no-feed beta).
  // Shown only once ≥2 NAV snapshots make a window computable.
  let tiles: PeriodTiles | null = null;
  try {
    tiles = await getPerformanceTiles(tenantId, id, accountIds);
  } catch {
    tiles = null;
  }
  const showTiles = tiles?.tiles.some((t) => t.twr != null || t.gain != null) ?? false;

  // Markets strip: intraday major-index proxies (feed-gated → Pro). Fetched server-side
  // for first paint ONLY when entitled (hidden in the no-feed beta, per metron-ops#53);
  // the client component then polls every ~5 min. Best-effort — never blocks the page.
  const indicesEnt = featureEntitlement(entitlements, "indices");
  const indices = indicesEnt?.available
    ? await getIndices(tenantId, previewFromCookies()).catch(() => null)
    : null;

  return (
    <div>
      <PortfolioNav portfolioId={id} name={portfolio.name} navQuery={navQuery} plugins={plugins} featureStates={featureStates} />
      {entitlements ? <TierSimulator entitlements={entitlements} /> : null}

      <div className="mt-3">
        <RenamePortfolio portfolioId={id} name={portfolio.name} />
      </div>

      {/* Markets (intraday index proxies) — feed-gated (Pro), hidden in the no-feed beta
          (metron-ops#53). Macro moved back to the Macro page (metron-ops#83). */}
      {indices?.available ? <IndexStrip initial={indices} /> : null}

      {/* Portfolio performance vs market (metron-ops#83): Today / YTD / LTM × $ gain, %TWR,
          and per-benchmark alpha (feed-gated). Sits directly under Markets so the two read
          in comparable TWR terms, above the Total value headline. */}
      {showTiles && tiles ? (
        <PerfTiles tiles={tiles.tiles} benchmarksAvailable={tiles.benchmarks_available} sessionHref={`/portfolios/${id}/holdings?val=live`} />
      ) : null}

      {/* Headline: total value, with unrealized broken out by tax treatment. */}
      {priced ? (
        <div className="mt-6 rounded-lg border border-line p-5">
          <div className="flex items-baseline justify-between gap-2">
            {/* This page always shows the whole portfolio (scoped === false), so no
                "n of m accounts" subtitle — every account contributes to this headline. */}
            <div className="text-xs uppercase tracking-wide text-muted">Total value</div>
            {/* The Overview is SETTLED (metron-ops#154): the headline is the official
                EOD-close NAV; live session detail lives in Holdings' live mode. The quiet
                slow poll below still catches the daily snapshot advance in an open tab. */}
            <span className="text-[11px] text-muted" title="Settled view — the official end-of-day close valuation. Live session detail is on Holdings → Live session.">
              settled close
            </span>
            <SettledRefresher />
          </div>
          <div className="mt-1 text-3xl font-semibold tabular-nums">{moneyWhole(summary.market_value as number, ccy)}</div>
          <div className="mt-1 text-xs text-muted">cost basis {moneyWhole(summary.total_cost_basis, ccy)}</div>
          {/* Unrealized split by tax treatment — needs per-account data, so it streams. */}
          <Suspense fallback={<SplitCardsSkeleton />}>
            <UnrealizedSplit tenantId={tenantId} id={id} ccy={ccy} accountIds={accountIds} scoped={scoped} navQuery={navQuery} />
          </Suspense>
        </div>
      ) : (
        <div className="mt-6">
          <StatCard label="Cost basis" value={moneyWhole(summary.total_cost_basis, ccy)} hint={`${summary.n_holdings} holdings`} href={`/portfolios/${id}/holdings${navQuery}`} />
        </div>
      )}

      {/* Performance over time (moved here from Holdings #113): per-account NAV lines +
          feed-gated benchmark overlays. The trend centerpiece of the dashboard. */}
      <Suspense fallback={<SectionSkeleton rows={4} />}>
        <PerfChartSection tenantId={tenantId} id={id} accountIds={accountIds} />
      </Suspense>

      {/* Realized YTD — splits the same way as unrealized: taxable carries the tax
          consequence (with the ST/LT breakdown), tax-advantaged is never taxed. */}
      <div className="mt-4 rounded-lg border border-line p-5">
        <div className="text-xs uppercase tracking-wide text-muted">Realized YTD</div>
        {/* Values come from `summary`, but the "—"-vs-value gating needs to know whether the
            selection holds taxable / tax-advantaged accounts — so this streams. */}
        <Suspense fallback={<SplitCardsSkeleton className="mt-3" />}>
          <RealizedYtdSplit
            tenantId={tenantId}
            id={id}
            ccy={ccy}
            accountIds={accountIds}
            scoped={scoped}
            navQuery={navQuery}
            realizedYtdTaxable={realizedYtdTaxable}
            realizedStYtd={summary.realized_st_ytd}
            realizedLtYtd={summary.realized_lt_ytd}
            realizedYtdTaxadv={summary.realized_ytd_taxadv}
          />
        </Suspense>
      </div>

      {/* Composition + movers (moved here from Holdings #113): allocation by country/sector
          and best/worst holdings, whole-portfolio. Fetches holdings once for both. */}
      <Suspense fallback={<SectionSkeleton rows={4} />}>
        <HoldingsAggregatesSection tenantId={tenantId} id={id} ccy={ccy} accountIds={accountIds} priced={priced} />
      </Suspense>

      {/* Holdings / accounts counts → their pages. Accounts are now managed on Holdings. */}
      <div className="mt-4 grid grid-cols-2 gap-3">
        <StatCard
          label="Holdings"
          value={String(summary.n_holdings)}
          hint="open positions"
          href={`/portfolios/${id}/holdings${navQuery}`}
        />
        <StatCard
          label="Accounts"
          value={String(summary.n_accounts)}
          hint="manage / activate"
          href={`/portfolios/${id}/holdings${navQuery}`}
        />
      </div>

      {summary.n_unconverted > 0 ? (
        <p className="mt-2 text-xs text-muted">
          {summary.n_unconverted} foreign holding{summary.n_unconverted === 1 ? "" : "s"} excluded from the{" "}
          {ccy} totals — no FX rate cached yet. Refresh prices to fetch it.
        </p>
      ) : null}
    </div>
  );
}

// --- streamed, accounts/holdings-dependent regions --------------------------
// Each fetches its own slice; #129's server-side single-flight cache collapses the
// parallel `getAccounts` reads into one upstream call (and is instant on repeat visits).

/** Active selection of accounts for this page (empty selection = whole portfolio). */
async function loadActiveAccounts(
  tenantId: string,
  id: string,
  accountIds: string[],
  scoped: boolean,
): Promise<Account[] | null> {
  const accounts = await getAccounts(tenantId, id).catch(() => null);
  if (!accounts) return null;
  return scoped ? accounts.filter((a) => accountIds.includes(a.account_id)) : accounts;
}

/** Pulsing placeholder for the two-card split sections while accounts load. */
function SplitCardsSkeleton({ className = "mt-4" }: { className?: string }) {
  return (
    <div className={`grid grid-cols-1 gap-3 sm:grid-cols-2 ${className}`} aria-busy="true">
      <div className="h-20 animate-pulse rounded-md bg-line/60" />
      <div className="h-20 animate-pulse rounded-md bg-line/60" />
    </div>
  );
}

/** Skeleton fallback for a streamed Section (chart / aggregates) while its data loads. */
function SectionSkeleton({ rows = 3 }: { rows?: number }) {
  return (
    <section className="mt-8 animate-pulse" aria-busy="true">
      <div className="h-3 w-32 rounded bg-line/60" />
      <div className="mt-3 space-y-2">
        {Array.from({ length: rows }).map((_, i) => (
          <div key={i} className="h-10 w-full rounded bg-line/60" />
        ))}
      </div>
    </section>
  );
}

async function UnrealizedSplit({
  tenantId, id, ccy, accountIds, scoped, navQuery,
}: {
  tenantId: string; id: string; ccy: string; accountIds: string[]; scoped: boolean; navQuery: string;
}) {
  const activeAccts = await loadActiveAccounts(tenantId, id, accountIds, scoped);
  if (!activeAccts) return <SplitCardsSkeleton />;
  // Unrealized split by tax treatment (metron-ops#64): never sum unrealized across
  // treatments — gains in an IRA/401(k)/Roth are never taxed, so the taxable figure is the
  // only one with a tax consequence.
  const taxableUnreal = sumOrNull(activeAccts.filter(isTaxable), (a) => a.unrealized_gain);
  const advUnreal = sumOrNull(activeAccts.filter((a) => !isTaxable(a)), (a) => a.unrealized_gain);
  // Settled by construction (metron-ops#154): both the MV leg and the cost basis are
  // official-close figures now, so the old live-blend disclosure (metron-ops#147) is gone.
  return (
    <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2">
      <Link href={`/portfolios/${id}/tax${navQuery}`} className="rounded-md border border-line p-3 transition hover:border-muted hover:bg-white/5">
        <div className="text-xs uppercase tracking-wide text-muted">Taxable unrealized →</div>
        <div className={`mt-1 text-xl font-semibold tabular-nums ${signClass(taxableUnreal ?? 0)}`}>
          {taxableUnreal != null ? accountingMoneyWhole(taxableUnreal, ccy) : "—"}
        </div>
        <div className="mt-1 text-xs text-muted">the only unrealized with a tax consequence</div>
      </Link>
      <div className="rounded-md border border-line/60 p-3">
        <div className="text-xs uppercase tracking-wide text-muted/70">Tax-advantaged unrealized</div>
        <div className="mt-1 text-xl font-semibold tabular-nums text-muted">
          {advUnreal != null ? accountingMoneyWhole(advUnreal, ccy) : "—"}
        </div>
        <div className="mt-1 text-xs text-muted/70">IRA / 401(k) / Roth — never taxed</div>
      </div>
    </div>
  );
}

/** Whole-portfolio performance chart (moved here from Holdings #113). Renders nothing until
 *  ≥1 account has ≥2 NAV points. */
async function PerfChartSection({
  tenantId, id, accountIds,
}: {
  tenantId: string; id: string; accountIds: string[];
}) {
  const perfSeries = await getHoldingsPerformanceSeries(tenantId, id, accountIds).catch(
    (): HoldingsPerfSeries | null => null,
  );
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

/** Allocation (country/sector) + best/worst performers (moved here from Holdings #113),
 *  whole-portfolio. Fetches holdings once and feeds both. */
async function HoldingsAggregatesSection({
  tenantId, id, ccy, accountIds, priced,
}: {
  tenantId: string; id: string; ccy: string; accountIds: string[]; priced: boolean;
}) {
  const holdings = await getHoldings(tenantId, id, accountIds).catch((): Holding[] | null => null);
  if (!holdings || holdings.length === 0) return null;
  return (
    <>
      {priced ? (
        <Section title="Allocation" note="by market value">
          <AllocationBreakdown holdings={holdings} baseCurrency={ccy} />
        </Section>
      ) : null}
      {priced ? (
        <Section title="Performers" note="best & worst holdings by return">
          <TopBottomPerformers holdings={holdings} />
        </Section>
      ) : null}
    </>
  );
}

async function RealizedYtdSplit({
  tenantId, id, ccy, accountIds, scoped, navQuery,
  realizedYtdTaxable, realizedStYtd, realizedLtYtd, realizedYtdTaxadv,
}: {
  tenantId: string; id: string; ccy: string; accountIds: string[]; scoped: boolean; navQuery: string;
  realizedYtdTaxable: number; realizedStYtd: number; realizedLtYtd: number; realizedYtdTaxadv: number;
}) {
  const activeAccts = await loadActiveAccounts(tenantId, id, accountIds, scoped);
  if (!activeAccts) return <SplitCardsSkeleton className="mt-3" />;
  // Realized YTD splits the same way as unrealized. Show "—" for a treatment the active
  // selection holds no accounts of (mirrors the unrealized cards).
  const hasTaxableAcct = activeAccts.some(isTaxable);
  const hasAdvAcct = activeAccts.some((a) => !isTaxable(a));
  return (
    <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
      <Link href={`/portfolios/${id}/tax${navQuery}`} className="rounded-md border border-line p-3 transition hover:border-muted hover:bg-white/5">
        <div className="text-xs uppercase tracking-wide text-muted">Taxable realized YTD →</div>
        <div className={`mt-1 text-xl font-semibold tabular-nums ${signClass(realizedYtdTaxable)}`}>
          {hasTaxableAcct ? accountingMoneyWhole(realizedYtdTaxable, ccy) : "—"}
        </div>
        <div className="mt-1 text-xs text-muted">
          {hasTaxableAcct
            ? `short-term ${signedMoneyWhole(realizedStYtd, ccy)} · long-term ${signedMoneyWhole(realizedLtYtd, ccy)}`
            : "the realized gains with a tax consequence"}
        </div>
      </Link>
      <div className="rounded-md border border-line/60 p-3">
        <div className="text-xs uppercase tracking-wide text-muted/70">Tax-advantaged realized YTD</div>
        <div className="mt-1 text-xl font-semibold tabular-nums text-muted">
          {hasAdvAcct ? accountingMoneyWhole(realizedYtdTaxadv, ccy) : "—"}
        </div>
        <div className="mt-1 text-xs text-muted/70">IRA / 401(k) / Roth — no tax consequence</div>
      </div>
    </div>
  );
}
