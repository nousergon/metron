import { acctParams, getAccounts, getIndices, getMacro, getPlugins, getPortfolio, getSummary, MetronApiError, type Account, type Portfolio, type PluginNav } from "@/lib/api";
import { moneyWhole, signClass, signedMoneyWhole } from "@/lib/format";
import { Empty, Section, StatCard } from "@/components/ui";
import { AccountPanel } from "@/components/account-panel";
import { PortfolioNav } from "@/components/portfolio-nav";
import { TierSimulator } from "@/components/tier-simulator";
import { MacroStrip } from "@/components/macro-strip";
import { IndexStrip } from "@/components/index-strip";
import { RenamePortfolio } from "@/components/rename-portfolio";
import { featureEntitlement, loadEntitlements, previewFromCookies, toFeatureStates } from "@/lib/entitlements";
import { requireTenantId } from "@/lib/session";
import { resolveAccountIds } from "@/lib/selection";
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
  searchParams,
}: {
  params: { id: string };
  searchParams: { account_id?: string | string[] };
}) {
  const { id } = params;
  const tenantId = await requireTenantId();

  // The account selection (repeatable ?account_id=); empty = whole portfolio. URL wins;
  // with none, the saved panel selection is applied (redirect). Activation is managed on
  // the Holdings page (metron-ops#64) — here the Overview metrics follow that selection.
  const accountIds = await resolveAccountIds(tenantId, id, `/portfolios/${id}`, searchParams.account_id);
  const scoped = accountIds.length > 0;
  const navQuery = acctParams(accountIds);

  let portfolio: Portfolio, summary, accounts;
  try {
    [portfolio, summary, accounts] = await Promise.all([
      getPortfolio(tenantId, id),
      getSummary(tenantId, id, accountIds),
      getAccounts(tenantId, id),
    ]);
  } catch (e) {
    if (e instanceof MetronApiError && e.status === 404) {
      return <Empty>Portfolio not found.</Empty>;
    }
    return <Empty>Couldn&apos;t load this portfolio. Is the backend running?</Empty>;
  }

  const ccy = summary.base_currency;
  const priced = summary.market_value != null;

  // Unrealized split by tax treatment (metron-ops#64): never sum unrealized across
  // treatments — gains in an IRA/401(k)/Roth are never taxed, so the taxable figure is the
  // only one with a tax consequence. Compute over the ACTIVE selection (what the metrics
  // reflect); empty selection = whole portfolio.
  const activeAccts = scoped ? accounts.filter((a) => accountIds.includes(a.account_id)) : accounts;
  const taxableUnreal = sumOrNull(activeAccts.filter(isTaxable), (a) => a.unrealized_gain);
  const advUnreal = sumOrNull(activeAccts.filter((a) => !isTaxable(a)), (a) => a.unrealized_gain);

  // Premium nav (metron-ops). Best-effort + always empty on the public tier.
  let plugins: PluginNav[] = [];
  try {
    plugins = await getPlugins(tenantId);
  } catch {
    plugins = [];
  }

  // Macro snapshot (FRED, public-domain → beta-safe). Best-effort (metron-ops#49).
  const macro = await getMacro(tenantId).catch(() => null);

  const entitlements = await loadEntitlements(tenantId);
  const featureStates = toFeatureStates(entitlements);

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

      {/* Markets (intraday index proxies) + Macro at the top of the dashboard. The
          markets strip is feed-gated (Pro) — hidden in the no-feed beta (metron-ops#53). */}
      {indices?.available ? <IndexStrip initial={indices} /> : null}
      {macro ? <MacroStrip macro={macro} portfolioId={id} /> : null}

      {/* Headline: total value, with unrealized broken out by tax treatment. */}
      {priced ? (
        <div className="mt-6 rounded-lg border border-line p-5">
          <div className="text-xs uppercase tracking-wide text-muted">Total value</div>
          <div className="mt-1 text-3xl font-semibold tabular-nums">{moneyWhole(summary.market_value as number, ccy)}</div>
          <div className="mt-1 text-xs text-muted">cost basis {moneyWhole(summary.total_cost_basis, ccy)}</div>
          <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2">
            <Link href={`/portfolios/${id}/tax${navQuery}`} className="rounded-md border border-line p-3 transition hover:border-muted hover:bg-white/5">
              <div className="text-xs uppercase tracking-wide text-muted">Taxable unrealized →</div>
              <div className={`mt-1 text-xl font-semibold tabular-nums ${signClass(taxableUnreal ?? 0)}`}>
                {taxableUnreal != null ? signedMoneyWhole(taxableUnreal, ccy) : "—"}
              </div>
              <div className="mt-1 text-xs text-muted">the only unrealized with a tax consequence</div>
            </Link>
            <div className="rounded-md border border-line/60 p-3">
              <div className="text-xs uppercase tracking-wide text-muted/70">Tax-advantaged unrealized</div>
              <div className="mt-1 text-xl font-semibold tabular-nums text-muted">
                {advUnreal != null ? signedMoneyWhole(advUnreal, ccy) : "—"}
              </div>
              <div className="mt-1 text-xs text-muted/70">IRA / 401(k) / Roth — never taxed</div>
            </div>
          </div>
        </div>
      ) : (
        <div className="mt-6">
          <StatCard label="Cost basis" value={moneyWhole(summary.total_cost_basis, ccy)} hint={`${summary.n_holdings} holdings`} href={`/portfolios/${id}/holdings${navQuery}`} />
        </div>
      )}

      {/* Consolidated metric tiles — each links into its source (metron-ops#64). */}
      <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
        <StatCard
          label="Realized gains"
          value={signedMoneyWhole(summary.realized_total, ccy)}
          valueClass={signClass(summary.realized_total)}
          hint="short + long term"
          href={`/portfolios/${id}/tax${navQuery}`}
        />
        <StatCard
          label="Income"
          value={moneyWhole(summary.dividends + summary.interest, ccy)}
          hint="dividends + interest"
          href={`/portfolios/${id}/tax${navQuery}`}
        />
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

      {/* Accounts summary (read-only) — activation lives on the Holdings page. */}
      <Section title="Accounts" note={scoped ? `${summary.n_accounts} of ${accounts.length} active` : undefined}>
        <AccountPanel accounts={accounts} baseCurrency={ccy} portfolioId={id} readOnly />
        <p className="mt-2 text-xs text-muted">
          <Link href={`/portfolios/${id}/holdings${navQuery}`} className="text-accent hover:underline">
            Manage &amp; activate accounts on the Holdings page →
          </Link>
        </p>
      </Section>
    </div>
  );
}
