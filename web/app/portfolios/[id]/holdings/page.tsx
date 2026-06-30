import { Suspense } from "react";
import { acctParams, getAccounts, getHoldings, getSummary, getValuationMedians, MetronApiError, type Entitlements, type Holding, type Summary, type ValuationMedians } from "@/lib/api";
import { Empty, Section } from "@/components/ui";
import { AccountPanel } from "@/components/account-panel";
import { HoldingsView } from "@/components/holdings-view";
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
  // Combine across accounts (metron-ops#114): default consolidates one row per ticker;
  // `?combine=accounts` shows the uncombined view (one row per account-position, +Account
  // column). URL-driven like the account selection — it changes the holdings DATA shape, so
  // it belongs server-side (column presets + grouping stay client-side / presentational).
  const byAccount = searchParams.combine === "accounts";

  // Phase A — everything independent of the resolved account selection, in parallel.
  // Holdings defaults to ALL accounts (metron-ops#113): with no explicit ?account_id= the
  // page anchors on the whole portfolio rather than restoring a saved subset — the account
  // panel still filters within the session via the URL.
  const [featureStates, entitlements, accountIds] = await Promise.all([
    navFeatureStates(tenantId),
    loadEntitlements(tenantId),
    resolveAccountIds(tenantId, id, `/portfolios/${id}/holdings`, searchParams.account_id, { applySaved: false }),
  ]);
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
        <HoldingsSection tenantId={tenantId} id={id} accountIds={accountIds} ccy={ccy} priced={priced} entitlements={entitlements} byAccount={byAccount} />
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

async function HoldingsSection({
  tenantId, id, accountIds, ccy, priced, entitlements, byAccount,
}: {
  tenantId: string; id: string; accountIds: string[]; ccy: string; priced: boolean; entitlements: Entitlements | null; byAccount: boolean;
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
        <HoldingsView holdings={holdings} baseCurrency={ccy} priced={priced} medians={medians} portfolioId={id} byAccount={byAccount} />
      )}
    </Section>
  );
}
