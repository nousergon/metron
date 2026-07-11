import { Suspense } from "react";
import { acctParams, getAccounts, getHoldings, getHoldingsView, getIntradayLegs, getIntradayStatus, getSummary, getToday, getValuationMedians, getWatchlist, MetronApiError, type Entitlements, type Holding, type HoldingsViewPrefs, type IntradayLegHistory, type IntradayStatus, type Summary, type Today, type ValuationMedians, type WatchlistEntry } from "@/lib/api";
import { Empty, Section } from "@/components/ui";
import { HoldingsView } from "@/components/holdings-view";
import { LiveValuationProvider } from "@/components/live-valuation-context";
import { RefreshPrices } from "@/components/refresh-prices";
import { IntradayRefresher } from "@/components/intraday-refresher";
import { SettledRefresher } from "@/components/settled-refresher";
import { SessionPanel } from "@/components/session-panel";
import { PortfolioNav } from "@/components/portfolio-nav";
import { WatchlistCompareTable } from "@/components/watchlist-compare-table";
import { loadEntitlements, navFeatureStates } from "@/lib/entitlements";
import { requireApiAuth } from "@/lib/session";
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
  searchParams: { account_id?: string | string[]; combine?: string; val?: string };
}) {
  const { id } = params;
  const apiAuth = await requireApiAuth();

  // Phase A — everything independent of the resolved account selection, in parallel.
  // Holdings defaults to ALL accounts (metron-ops#113): with no explicit ?account_id= the
  // page anchors on the whole portfolio rather than restoring a saved subset — the account
  // panel still filters within the session via the URL. The saved view (metron-ops#114)
  // hydrates the toolbar so grouping / bands / combine survive reloads.
  const [featureStates, entitlements, accountIds, savedView, live] = await Promise.all([
    navFeatureStates(apiAuth),
    loadEntitlements(apiAuth),
    resolveAccountIds(apiAuth, id, `/portfolios/${id}`, searchParams.account_id, { applySaved: false }),
    getHoldingsView(apiAuth, id).catch((): HoldingsViewPrefs | null => null),
    getIntradayStatus(apiAuth, id).catch((): IntradayStatus | null => null),
  ]);

  // Valuation regime (metron-ops#153). Holdings is the ONLY surface with a live mode; the
  // regime is an explicit, visible selection — never an implicit blend. Resolution:
  // `?val=` (session) → saved view → default LIVE when available (parity with the old
  // always-live behavior for a user whose intraday toggle is on). Live is AVAILABLE only
  // when the deployment has the feed and the user's intraday toggle is on — reasons "off"
  // and "feed" mean neither will change within this session.
  const sessionState = live?.session_state ?? "closed";
  // Live is OFFERED only when feed + toggle allow it AND the session state has something
  // live mode can add ("live" in session, "recap" post-close same day). Pre-market /
  // weekend / holiday → the toggle grays and the regime clamps to settled even if the
  // saved preference is live (metron-ops-I156).
  const liveAvailable = !!live && live.reason !== "off" && live.reason !== "feed";
  const liveOffered = liveAvailable && sessionState !== "closed";
  const requested =
    searchParams.val === "live" || searchParams.val === "settled"
      ? searchParams.val
      : savedView?.valuation === "live" || savedView?.valuation === "settled"
        ? savedView.valuation
        : "live";
  const valuation: "live" | "settled" = liveOffered && requested === "live" ? "live" : "settled";

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
  // (accounts / holdings) stream below. Valued per the resolved regime.
  let summary: Summary;
  try {
    summary = await getSummary(apiAuth, id, accountIds, valuation);
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
        {/* Live mode: position values revalue from intraday balances every ~5 min while
            open (#79), with the honest coverage label. Settled mode: a quiet slow poll so
            an all-day-open tab still catches the EOD snapshot advance (metron-ops#154). */}
        {valuation === "live" ? <IntradayRefresher portfolioId={id} /> : <SettledRefresher />}
      </div>
      <p className="text-sm text-muted">
        All accounts are included by default. (De)activate accounts below to filter the positions for this view.
      </p>

      {/* Live-session panel (metron-ops#153): coverage banner + covered-basis session strip
          + excluded-holdings disclosure. Live mode only — the settled regime shows no
          session figures anywhere on the page. */}
      {valuation === "live" && priced && live ? (
        <Suspense fallback={<SectionSkeleton rows={2} />}>
          <SessionSection apiAuth={apiAuth} id={id} ccy={ccy} accountIds={accountIds} status={live} />
        </Suspense>
      ) : null}

      <Suspense fallback={<SectionSkeleton rows={6} />}>
        <HoldingsSection apiAuth={apiAuth} id={id} accountIds={accountIds} ccy={ccy} priced={priced} entitlements={entitlements} byAccount={byAccount} savedView={savedView} valuation={valuation} liveAvailable={liveAvailable} sessionState={sessionState} />
      </Suspense>

      <Suspense fallback={<SectionSkeleton rows={3} />}>
        <WatchlistSection apiAuth={apiAuth} id={id} ccy={ccy} />
      </Suspense>
    </div>
  );
}

// --- streamed sections -----------------------------------------------------

/** The live-session panel's data (scoped like the table): the covered-basis Today
 *  decomposition + the drift history, joined with the coverage status fetched upstream. */
async function SessionSection({
  apiAuth, id, ccy, accountIds, status,
}: {
  apiAuth: string; id: string; ccy: string; accountIds: string[]; status: IntradayStatus;
}) {
  const [today, legs] = await Promise.all([
    getToday(apiAuth, id, accountIds).catch((): Today | null => null),
    getIntradayLegs(apiAuth, id).catch((): IntradayLegHistory | null => null),
  ]);
  if (!today) return null;
  return <SessionPanel status={status} today={today} legs={legs} ccy={ccy} />;
}

async function HoldingsSection({
  apiAuth, id, accountIds, ccy, priced, entitlements, byAccount, savedView, valuation, liveAvailable, sessionState,
}: {
  apiAuth: string; id: string; accountIds: string[]; ccy: string; priced: boolean; entitlements: Entitlements | null; byAccount: boolean; savedView: HoldingsViewPrefs | null; valuation: "live" | "settled"; liveAvailable: boolean; sessionState: "live" | "recap" | "closed";
}) {
  let holdings: Holding[];
  try {
    holdings = await getHoldings(apiAuth, id, accountIds, byAccount, valuation);
  } catch {
    return (
      <Section title="Holdings">
        <Empty>Couldn&apos;t load holdings.</Empty>
      </Section>
    );
  }
  // SP1500-broad sector/country median bands — best-effort + feed-gated. Accounts feed
  // the toolbar scope chip (metron-ops-I156) — the panel moved to the Overview.
  const [medians, accounts] = await Promise.all([
    getValuationMedians(apiAuth, id, accountIds).catch((): ValuationMedians | null => null),
    getAccounts(apiAuth, id).catch(() => null),
  ]);
  // Provenance-honest header (metron-ops#146/#153): the regime is explicit. Live mode may
  // claim intraday freshness only while the overlay actually applies (post-close the same
  // mode reads "as of close"); settled mode names the close date it's valued at. On a
  // failed status read, claim the conservative settled state.
  const live: IntradayStatus | null =
    priced && valuation === "live"
      ? await getIntradayStatus(apiAuth, id).catch((): IntradayStatus | null => null)
      : null;
  // The settled as-of date, from the data itself (the freshest priced date on the page —
  // the close date for close-fed rows) — static copy never makes freshness claims
  // (metron-ops#145).
  const settledAsOf = holdings
    .map((h) => h.last_price_date)
    .filter((d): d is string => d != null)
    .sort()
    .pop();
  const valuationNote = live?.applied
    ? `all values in ${ccy} · market value ~15-min delayed intraday`
    : valuation === "live"
      ? `all values in ${ccy} · session closed — market value as of last close`
      : `all values in ${ccy} · settled at ${settledAsOf ?? "last"} close`;

  return (
    <Section title="Holdings" note={priced ? valuationNote : "cost basis — refresh for market value"}>
      <div className="mb-3">
        <RefreshPrices portfolioId={id} feedOn={entitlements?.feed_enabled} />
      </div>
      {holdings.length === 0 ? (
        <Empty>No open positions.</Empty>
      ) : (
        // The provider carries the overlay state to the table's live/close provenance
        // markers (metron-ops#147) — settled mode mounts it with live=false so the table
        // makes zero live claims; the Watchlist section below stays outside it entirely.
        <LiveValuationProvider live={valuation === "live" && (live?.applied ?? false)}>
          <HoldingsView holdings={holdings} baseCurrency={ccy} priced={priced} medians={medians} portfolioId={id} byAccount={byAccount} savedGrouping={savedView?.grouping ?? null} savedHiddenTypes={savedView?.hidden_types ?? null} valuation={valuation} liveAvailable={liveAvailable} sessionState={sessionState} accounts={accounts ?? undefined} selectedAccountIds={accountIds} />
        </LiveValuationProvider>
      )}
    </Section>
  );
}

// Watchlist (metron-ops#42/#121) — a comparison-only table of tracked tickers you don't
// (necessarily) hold, sortable on the same metrics as the Holdings table above. Always
// scoped to the whole portfolio (not account-filtered — a watchlist entry has no account).
async function WatchlistSection({ apiAuth, id, ccy }: { apiAuth: string; id: string; ccy: string }) {
  let entries: WatchlistEntry[];
  try {
    entries = await getWatchlist(apiAuth, id);
  } catch {
    return null; // best-effort — the Holdings table above already rendered
  }
  return (
    <Section title="Watchlist" note="tracked tickers, compared side-by-side · metrics as of last close — never affects NAV or performance">
      <WatchlistCompareTable portfolioId={id} baseCurrency={ccy} entries={entries} />
    </Section>
  );
}
