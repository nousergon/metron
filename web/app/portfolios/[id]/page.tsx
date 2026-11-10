import { Suspense } from "react";
import { acctParams, getAccounts, getHoldings, getIndices, getMarketNote, getPerformanceTiles, type Indices, type PeriodTiles, getHoldingsView, getIntradayLegs, getIntradayStatus, getSummary, getToday, getValuationMedians, getWatchlist, MetronApiError, type Entitlements, type Holding, type HoldingsViewPrefs, type IntradayLegHistory, type IntradayStatus, type Summary, type Today, type ValuationMedians, type WatchlistEntry } from "@/lib/api";
import { Empty, Section } from "@/components/ui";
import { HoldingsView } from "@/components/holdings-view";
import { LiveValuationProvider } from "@/components/live-valuation-context";
import { RefreshPrices } from "@/components/refresh-prices";
import { IntradayRefresher } from "@/components/intraday-refresher";
import { SettledRefresher } from "@/components/settled-refresher";
import { SessionNotes, SessionPanel } from "@/components/session-panel";
import { FilteredPortfolioTotal, HiddenTypesProvider } from "@/components/holdings-filter-context";
import { PositionsAsOfCaption, PricesAsOfCaption } from "@/components/price-freshness";
import { PortfolioNav } from "@/components/portfolio-nav";
import { WatchlistCompareTable } from "@/components/watchlist-compare-table";
import { ColumnBandsProvider } from "@/components/column-bands-context";
import { DEFAULT_VISIBLE_GROUPS, INTRADAY_VISIBLE_GROUPS } from "@/components/holdings-column-presets";
import { featureEntitlement, loadEntitlements, navFeatureStates, previewFromCookies } from "@/lib/entitlements";
import { IndexStrip } from "@/components/index-strip";
import { PerfTiles } from "@/components/perf-tiles";
import { MarketNoteView } from "@/components/market-note";
import { MarketNoteLate } from "@/components/market-note-late";
import { RetryRefresh, RetryReset } from "@/components/retry-refresh";
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
//
// Landing layout (Brian, 2026-09-28, reviewed on a phone): the Portfolio total leads the
// page content; the live-session headline follows; then Holdings and Watchlist; and every
// explanatory NOTE (session footnotes, freshness captions, the account-scope hint) sits in
// one Notes block at the very bottom. The what-if / if-sold panels moved to their own page
// (./what-if). The holdings read is started ONCE here and shared by the three sections that
// need it (total, table, notes) — each awaits the same promise under its own <Suspense>.
export default async function HoldingsPage(
  props: {
    params: Promise<{ id: string }>;
    searchParams: Promise<{ account_id?: string | string[]; combine?: string; val?: string }>;
  }
) {
  const searchParams = await props.searchParams;
  const params = await props.params;
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
  // Live is OFFERED whenever feed + toggle allow it, at EVERY session state — including
  // "closed" (pre-market / weekend / holiday). Supersedes part of metron-ops-I156 (Brian,
  // 2026-07-22): the prior rule clamped to settled once closed on the theory that "a control
  // must never imply live-ness after hours." The new rule keeps the live regime resolvable
  // and FREEZES it instead — the same data path recap already uses (day/session legs come
  // from the last-written intraday snapshot regardless of its age; see
  // security_perf.enrich_holdings + intraday.today_view) stays live indefinitely, honestly
  // labeled as stale via ValuationToggle's "Last session" copy and SessionPanel's
  // as-of-close framing, rather than the whole page vanishing back to settled.
  const liveAvailable = !!live && live.reason !== "off" && live.reason !== "feed";
  const liveOffered = liveAvailable;
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

  // Summary gives the base currency + priced state the holdings sections need (and
  // resolves portfolio-not-found). It no longer blocks the shell (Brian, 2026-11-09: "the
  // page materializes with what can load quickly"): the nav, the stats strip and the note
  // paint at once, and each section that needs the basis awaits this promise itself.
  const basisP: Promise<Basis> = getSummary(apiAuth, id, accountIds, valuation).then(
    (summary: Summary): Basis => ({ ok: true, ccy: summary.base_currency, priced: summary.market_value != null }),
    (e: unknown): Basis => ({ ok: false, notFound: e instanceof MetronApiError && e.status === 404 }),
  );

  // One holdings read shared by the total, the table and the notes (see header comment).
  // Each consumer handles a rejection itself; the no-op catch only keeps an unconsumed
  // rejection (e.g. a streamed boundary abandoned mid-render) from surfacing as unhandled.
  const holdingsP = getHoldings(apiAuth, id, accountIds, byAccount, valuation);
  holdingsP.catch(() => {});
  // The live session's data (covered-basis Today + drift legs), shared by the headline
  // panel near the top and its footnotes in the bottom notes. Live mode only.
  const sessionP: Promise<SessionData> | null =
    valuation === "live" && live
      ? Promise.all([
          getToday(apiAuth, id, accountIds).catch((): Today | null => null),
          getIntradayLegs(apiAuth, id).catch((): IntradayLegHistory | null => null),
        ]).then(([today, legs]) => ({ today, legs }))
      : null;

  return (
    // Seeded from the saved view so the hoisted total's first render matches the table.
    <HiddenTypesProvider initialHidden={savedView?.hidden_types ?? null}>
    <div>
      <PortfolioNav portfolioId={id} navQuery={navQuery} featureStates={featureStates} />

      <div className="mt-3 flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <h1 className="text-base font-semibold sm:text-lg">Holdings</h1>
        {/* Live mode: position values revalue from intraday balances every ~5 min while
            open (#79), with the honest coverage label. Settled mode: a quiet slow poll so
            an all-day-open tab still catches the EOD snapshot advance (metron-ops#154). */}
        {valuation === "live" ? <IntradayRefresher portfolioId={id} /> : <SettledRefresher />}
      </div>

      {/* Market + portfolio stats at the top (Brian, 2026-11-04): the five indexes and this
          portfolio over Today / MTD / YTD / LTM. Streamed so it never delays the table. */}
      <Suspense fallback={<Bar className="mt-3 h-24 w-full animate-pulse" />}>
        <StatsSection apiAuth={apiAuth} id={id} accountIds={accountIds} entitlements={entitlements} />
      </Suspense>

      {/* Portfolio vs market note (Brian, 2026-11-04): made only when this page is opened,
          streamed under its own boundary so a slow note never holds up anything else. */}
      <Suspense fallback={null}>
        <MarketNoteSection apiAuth={apiAuth} id={id} accountIds={accountIds} />
      </Suspense>

      {/* Portfolio total — first on the page (Brian, 2026-09-28). Same type-filtered rows as
          the table below (HiddenTypesProvider), so the two can never disagree. */}
      <Suspense fallback={<Bar className="mt-3 h-12 w-full animate-pulse" />}>
        <TotalSection holdingsP={holdingsP} basisP={basisP} />
      </Suspense>

      {/* Live-session panel (metron-ops#153): coverage banner + covered-basis session strip
          (its footnotes — covered basis, excluded holdings, drift split — are in the bottom
          Notes block since 2026-09-28). Live mode only — the settled regime shows no
          session figures anywhere on the page. Now mounts through "closed" too (the live
          regime is offered at every session state as of 2026-07-22): SessionPanel and its
          CoverageBanner were already written generically for "stale → as of close" framing,
          so no change was needed here beyond letting valuation stay "live" that long. */}
      {sessionP && live ? (
        <Suspense fallback={<SectionSkeleton rows={2} />}>
          <SessionSection sessionP={sessionP} basisP={basisP} status={live} />
        </Suspense>
      ) : null}

      {/* ColumnBandsProvider (metron-ops#121 sync fix) wraps BOTH streamed sections below so
          the Holdings COLUMNS control's selection also drives the Watchlist table further
          down the page — they're two separate async Server Components under independent
          <Suspense> boundaries (kept independent here for their own streaming; collapsing
          them would make Watchlist wait on the Holdings fetch), so a shared client context
          mounted above both is the bridge. The provider itself renders no DOM. Its initial
          selection follows the resolved regime (Brian, 2026-07-22): Intraday while live
          (including frozen-after-hours) is the landing view Brian actually checks day to
          day; Overview is the settled-mode fallback. */}
      <ColumnBandsProvider initialBands={valuation === "live" ? INTRADAY_VISIBLE_GROUPS : DEFAULT_VISIBLE_GROUPS}>
        <Suspense fallback={<SectionSkeleton rows={6} />}>
          <HoldingsSection apiAuth={apiAuth} id={id} holdingsP={holdingsP} accountIds={accountIds} basisP={basisP} entitlements={entitlements} byAccount={byAccount} savedView={savedView} valuation={valuation} liveAvailable={liveAvailable} sessionState={sessionState} />
        </Suspense>

        <Suspense fallback={<SectionSkeleton rows={3} />}>
          <WatchlistSection apiAuth={apiAuth} id={id} basisP={basisP} />
        </Suspense>
      </ColumnBandsProvider>

      {/* Notes — every explanatory footnote on the page, at the very bottom (Brian,
          2026-09-28), below Holdings and Watchlist rather than between the headline cards.
          Stale-feed WARNINGS are alerts, not notes, and stay inline above the table. */}
      <section className="mt-10 space-y-2 border-t border-line pt-4" aria-labelledby="holdings-notes">
        <h2 id="holdings-notes" className="text-[11px] font-medium uppercase tracking-wide text-muted">
          Notes
        </h2>
        {sessionP ? (
          <Suspense fallback={null}>
            <SessionNotesSection sessionP={sessionP} basisP={basisP} />
          </Suspense>
        ) : null}
        <Suspense fallback={null}>
          <FreshnessNotesSection holdingsP={holdingsP} basisP={basisP} />
        </Suspense>
        <p className="text-xs text-muted">
          All accounts are included by default. (De)activate accounts with the account chip in the Holdings toolbar to
          filter the positions for this view.
        </p>
      </section>
    </div>
    </HiddenTypesProvider>
  );
}

// --- streamed sections -----------------------------------------------------

type SessionData = { today: Today | null; legs: IntradayLegHistory | null };

/** The summary-derived basis every holdings section needs, resolved off the shell. */
type Basis = { ok: true; ccy: string; priced: boolean } | { ok: false; notFound: boolean };

/** The hoisted Portfolio total (top of the page) — fails soft (the table reports errors). */
async function TotalSection({ holdingsP, basisP }: { holdingsP: Promise<Holding[]>; basisP: Promise<Basis> }) {
  const [basis, holdings] = await Promise.all([basisP, holdingsP.catch((): Holding[] | null => null)]);
  if (!basis.ok || !holdings || holdings.length === 0) return null;
  const { ccy, priced } = basis;
  return (
    <div className="mt-3">
      <FilteredPortfolioTotal holdings={holdings} baseCurrency={ccy} priced={priced} />
    </div>
  );
}

/** The live-session headline (scoped like the table): the covered-basis Today
 *  decomposition joined with the coverage status fetched upstream. */
async function SessionSection({ sessionP, basisP, status }: { sessionP: Promise<SessionData>; basisP: Promise<Basis>; status: IntradayStatus }) {
  const [basis, { today }] = await Promise.all([basisP, sessionP]);
  if (!basis.ok || !basis.priced || !today) return null;
  return <SessionPanel status={status} today={today} ccy={basis.ccy} />;
}

/** The live session's footnotes, rendered in the bottom Notes block. */
async function SessionNotesSection({ sessionP, basisP }: { sessionP: Promise<SessionData>; basisP: Promise<Basis> }) {
  const [basis, { today, legs }] = await Promise.all([basisP, sessionP]);
  if (!basis.ok || !basis.priced || !today) return null;
  return <SessionNotes today={today} legs={legs} ccy={basis.ccy} />;
}

/** The plain "prices as of" / "positions synced through" captions, over the whole holdings
 *  read (page-level provenance, independent of the table's type filter). */
async function FreshnessNotesSection({ holdingsP, basisP }: { holdingsP: Promise<Holding[]>; basisP: Promise<Basis> }) {
  const [basis, holdings] = await Promise.all([basisP, holdingsP.catch((): Holding[] | null => null)]);
  if (!basis.ok || !holdings || holdings.length === 0) return null;
  const { priced } = basis;
  return (
    <>
      {priced ? <PricesAsOfCaption holdings={holdings} /> : null}
      <PositionsAsOfCaption holdings={holdings} />
    </>
  );
}

async function HoldingsSection({
  apiAuth, id, holdingsP, accountIds, basisP, entitlements, byAccount, savedView, valuation, liveAvailable, sessionState,
}: {
  apiAuth: string; id: string; holdingsP: Promise<Holding[]>; accountIds: string[]; basisP: Promise<Basis>; entitlements: Entitlements | null; byAccount: boolean; savedView: HoldingsViewPrefs | null; valuation: "live" | "settled"; liveAvailable: boolean; sessionState: "live" | "recap" | "closed";
}) {
  const basis = await basisP;
  if (!basis.ok && basis.notFound) return <Empty>Portfolio not found.</Empty>;
  let holdings: Holding[];
  try {
    if (!basis.ok) throw new Error("summary unavailable");
    holdings = await holdingsP;
  } catch {
    // A slow or briefly unavailable API (2026-11-09: the worker was being recycled) is
    // retried in the background rather than parked behind "reload in a minute".
    return (
      <Section title="Holdings">
        <Empty>Holdings are taking longer than usual. Retrying in the background…</Empty>
        <RetryRefresh retryKey={`holdings:${id}`} />
      </Section>
    );
  }
  const { ccy, priced } = basis;
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
      <RetryReset retryKey={`holdings:${id}`} />
      <div className="mb-3">
        <RefreshPrices portfolioId={id} feedOn={entitlements?.feed_enabled} />
      </div>
      {holdings.length === 0 ? (
        <Empty>No open positions.</Empty>
      ) : (
        // The provider carries the overlay state to the table's live/close provenance
        // markers (metron-ops#147) — settled mode mounts it with live=false so the table
        // makes zero live claims; the Watchlist section below stays outside it entirely.
        (<LiveValuationProvider live={valuation === "live" && (live?.applied ?? false)}>
          <HoldingsView holdings={holdings} baseCurrency={ccy} priced={priced} medians={medians} portfolioId={id} byAccount={byAccount} savedGrouping={savedView?.grouping ?? null} savedHiddenTypes={savedView?.hidden_types ?? null} valuation={valuation} liveAvailable={liveAvailable} sessionState={sessionState} accounts={accounts ?? undefined} hoistTotalAndNotes />
        </LiveValuationProvider>)
      )}
    </Section>
  );
}

// Watchlist (metron-ops#42/#121) — a comparison-only table of tracked tickers you don't
// (necessarily) hold, sortable on the same metrics as the Holdings table above. Always
// scoped to the whole portfolio (not account-filtered — a watchlist entry has no account).
async function WatchlistSection({ apiAuth, id, basisP }: { apiAuth: string; id: string; basisP: Promise<Basis> }) {
  let entries: WatchlistEntry[];
  try {
    entries = await getWatchlist(apiAuth, id);
  } catch {
    return null; // best-effort — the Holdings table above already rendered
  }
  const basis = await basisP;
  if (!basis.ok) return null;
  const { ccy } = basis;
  return (
    <Section title="Watchlist" note="tracked tickers, compared side-by-side · metrics as of last close — never affects NAV or performance">
      <WatchlistCompareTable portfolioId={id} baseCurrency={ccy} entries={entries} />
    </Section>
  );
}

/** Index strip + portfolio period tiles (Brian, 2026-11-04). Each read is best-effort: a
 *  failure hides that half rather than breaking Holdings. The index strip is feed-gated. */
async function MarketNoteSection({
  apiAuth,
  id,
  accountIds,
}: {
  apiAuth: string;
  id: string;
  accountIds: string[];
}) {
  const note = await getMarketNote(apiAuth, id, accountIds).catch(() => null);
  // Missed the streamed window: keep fetching in the background and swap the note in when
  // it lands, instead of asking the reader to reload (Brian, 2026-11-09).
  if (!note) return <MarketNoteLate portfolioId={id} accountIds={accountIds} />;
  return <MarketNoteView note={note} />;
}

async function StatsSection({
  apiAuth,
  id,
  accountIds,
  entitlements,
}: {
  apiAuth: string;
  id: string;
  accountIds: string[];
  entitlements: Entitlements | null;
}) {
  const indicesEnt = featureEntitlement(entitlements, "indices");
  const [indices, tiles] = await Promise.all([
    indicesEnt?.available
      ? getIndices(apiAuth, previewFromCookies()).catch((): Indices | null => null)
      : Promise.resolve(null),
    getPerformanceTiles(apiAuth, id, accountIds).catch((): PeriodTiles | null => null),
  ]);
  const showTiles = tiles?.tiles.some((t) => t.twr != null || t.mwr != null || t.gain != null) ?? false;
  if (!indices?.available && !showTiles) return null;
  return (
    <div>
      {indices?.available ? <IndexStrip initial={indices} /> : null}
      {showTiles && tiles ? (
        <PerfTiles tiles={tiles.tiles} benchmarksAvailable={tiles.benchmarks_available} />
      ) : null}
    </div>
  );
}
