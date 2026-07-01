import { Suspense } from "react";
import { acctParams, getAccounts, getHoldings, getHoldingsView, getSummary, getValuationMedians, getWatchlist, MetronApiError, type Entitlements, type Holding, type HoldingsViewPrefs, type Summary, type ValuationMedians, type WatchlistEntry } from "@/lib/api";
import { Empty, Section } from "@/components/ui";
import { AccountPanel } from "@/components/account-panel";
import { HoldingsView } from "@/components/holdings-view";
import { RefreshPrices } from "@/components/refresh-prices";
import { IntradayRefresher } from "@/components/intraday-refresher";
import { PortfolioNav } from "@/components/portfolio-nav";
import { WatchlistCompareTable } from "@/components/watchlist-compare-table";
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

// Holdings — the position-level data grid, separated from the Overview dashboard
// (metron-ops#64). The aggregates (performance chart, allocation, performers, today's P&L)
// moved to the Overview (metron-ops#113); this page is the accounts summary + the holdings
// table. Accounts default to ALL (the whole portfolio) and can be (de)activated to filter
// the positions below for this view.
//
// Streaming layout (perf): the shell paints immediately from the cheap `summary` read, then
// the two expensive sections (Accounts, Holdings table) each stream in behind their own
// <Suspense> as their data lands. Each streamed section is an async Server Component that
// fetches only its own slice and fails soft.
export default async function HoldingsPage({
  params,
  searchParams,
}: {
  params: { id: string };
  searchParams: { account_id?: string | string[]; combine?: string };
}) {
  const { id } = params;
  const tenantId = await requireTenantId();

  // Phase A — everything independent of the resolved account selection, in parallel.
  // Holdings defaults to ALL accounts (metron-ops#113): with no explicit ?account_id= the
  // page anchors on the whole portfolio rather than restoring a saved subset — the account
  // panel still filters within the session via the URL. The saved view (metron-ops#114)
  // hydrates the toolbar so grouping / bands / combine survive reloads.
  const [featureStates, entitlements, accountIds, savedView] = await Promise.all([
    navFeatureStates(tenantId),
    loadEntitlements(tenantId),
    resolveAccountIds(tenantId, id, `/portfolios/${id}/holdings`, searchParams.account_id, { applySaved: false }),
    getHoldingsView(tenantId, id).catch((): HoldingsViewPrefs | null => null),
  ]);

  // Combine across accounts (metron-ops#114): an explicit `?combine=` in the URL wins for the
  // session; on a fresh load (no param) the saved preference seeds it. `combine=accounts`
  // shows the uncombined view (one row per account-position, +Account column). URL-driven
  // because it changes the holdings DATA shape (fetched server-side); grouping + column
  // presets are client-side presentational state, hydrated from the saved view below.
  const byAccount =
    searchParams.combine === "accounts"
      ? true
      : searchParams.combine === "combined"
        ? false
        : (savedView?.combine_by_account ?? false);
  const scoped = accountIds.length > 0;
  const navQuery = acctParams(accountIds);

  // The one CHEAP scoped read blocks the shell (≈0.3s): summary gives base currency +
  // priced state used throughout (and resolves portfolio-not-found). The expensive reads
  // (accounts / holdings) stream below.
  let summary: Summary;
  try {
    summary = await getSummary(tenantId, id, accountIds);
  } catch (e) {
    if (e instanceof MetronApiError && e.status === 404) {
      return <Empty>Portfolio not found.</Empty>;
    }
    return <Empty>Couldn&apos;t load holdings. Is the backend running?</Empty>;
  }

  const ccy = summary.base_currency;
  const priced = summary.market_value != null;

  return (
    <div>
      <PortfolioNav portfolioId={id} navQuery={navQuery} featureStates={featureStates} />

      <div className="mt-3 flex items-baseline gap-2">
        <h1 className="text-lg font-semibold">Holdings</h1>
        {/* Position values revalue from intraday balances every ~5 min while open (#79). */}
        <IntradayRefresher portfolioId={id} />
      </div>
      <p className="text-sm text-muted">
        All accounts are included by default. (De)activate accounts below to filter the positions for this view.
      </p>

      <Suspense fallback={<SectionSkeleton rows={2} />}>
        <AccountsSection tenantId={tenantId} id={id} ccy={ccy} scoped={scoped} nSelected={summary.n_accounts} />
      </Suspense>

      <Suspense fallback={<SectionSkeleton rows={6} />}>
        <HoldingsSection tenantId={tenantId} id={id} accountIds={accountIds} ccy={ccy} priced={priced} entitlements={entitlements} byAccount={byAccount} savedView={savedView} />
      </Suspense>

      <Suspense fallback={<SectionSkeleton rows={3} />}>
        <WatchlistSection tenantId={tenantId} id={id} ccy={ccy} />
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
      <AccountPanel accounts={accounts} baseCurrency={ccy} portfolioId={id} deletable />
      {scoped ? (
        <p className="mt-2 text-xs text-muted">
          Showing {nSelected} of {accounts.length} account{accounts.length === 1 ? "" : "s"} — the holdings below
          reflect this selection.
        </p>
      ) : null}
    </Section>
  );
}

async function HoldingsSection({
  tenantId, id, accountIds, ccy, priced, entitlements, byAccount, savedView,
}: {
  tenantId: string; id: string; accountIds: string[]; ccy: string; priced: boolean; entitlements: Entitlements | null; byAccount: boolean; savedView: HoldingsViewPrefs | null;
}) {
  let holdings: Holding[];
  try {
    holdings = await getHoldings(tenantId, id, accountIds, byAccount);
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
    <Section title="Holdings" note={priced ? `all values in ${ccy} · market value from last EOD close` : "cost basis — refresh for market value"}>
      <div className="mb-3">
        <RefreshPrices portfolioId={id} feedOn={entitlements?.feed_enabled} />
      </div>
      {holdings.length === 0 ? (
        <Empty>No open positions.</Empty>
      ) : (
        <HoldingsView holdings={holdings} baseCurrency={ccy} priced={priced} medians={medians} portfolioId={id} byAccount={byAccount} savedGrouping={savedView?.grouping ?? null} savedBands={savedView?.visible_bands ?? null} savedHiddenTypes={savedView?.hidden_types ?? null} />
      )}
    </Section>
  );
}

// Watchlist (metron-ops#42/#123) — a comparison-only table of tracked tickers you don't
// (necessarily) hold, sortable on the same metrics as the Holdings table above. Always
// scoped to the whole portfolio (not account-filtered — a watchlist entry has no account).
async function WatchlistSection({ tenantId, id, ccy }: { tenantId: string; id: string; ccy: string }) {
  let entries: WatchlistEntry[];
  try {
    entries = await getWatchlist(tenantId, id);
  } catch {
    return null; // best-effort — the Holdings table above already rendered
  }
  return (
    <Section title="Watchlist" note="tracked tickers, compared side-by-side — never affects NAV or performance">
      <WatchlistCompareTable portfolioId={id} baseCurrency={ccy} entries={entries} />
    </Section>
  );
}
