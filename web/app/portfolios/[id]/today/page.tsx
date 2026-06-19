import { acctParams, getToday, MetronApiError } from "@/lib/api";
import { accountingMoneyWhole, isoDate, percent, quantity, signClass } from "@/lib/format";
import { Empty, Section, StatCard, Table } from "@/components/ui";
import { PortfolioNav } from "@/components/portfolio-nav";
import { IntradayRefresher } from "@/components/intraday-refresher";
import { navFeatureStates } from "@/lib/entitlements";
import { requireTenantId } from "@/lib/session";
import { resolveAccountIds } from "@/lib/selection";

export const dynamic = "force-dynamic";

// Today — the intraday trading view (metron-ops#23). Per holding: prior close, today's
// open, latest (~15-min delayed) price, and the overnight·intraday·day P&L decomposition
// (Day = Overnight + Intraday, in both % and base-$). Reads the intraday spine quotes via
// the API (feed-gated); outside market hours the snapshot is stale and rows read "as of
// close". Auto-refreshes every ~5 min while open (shared <IntradayRefresher>).

function num(n: number | null): string {
  return n != null ? n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : "—";
}

function pct(n: number | null): string {
  return n != null ? percent(n) : "—";
}

function gain(n: number | null, ccy: string): string {
  return n != null ? accountingMoneyWhole(n, ccy) : "—";
}

export default async function TodayPage({
  params,
  searchParams,
}: {
  params: { id: string };
  searchParams: { account_id?: string | string[] };
}) {
  const { id } = params;
  const tenantId = await requireTenantId();
  const featureStates = await navFeatureStates(tenantId);
  const accountIds = await resolveAccountIds(tenantId, id, `/portfolios/${id}/today`, searchParams.account_id);
  const navQuery = acctParams(accountIds);

  let today;
  try {
    today = await getToday(tenantId, id, accountIds);
  } catch (e) {
    if (e instanceof MetronApiError && e.status === 404) return <Empty>Portfolio not found.</Empty>;
    return <Empty>Couldn&apos;t load the Today view. Is the backend running?</Empty>;
  }

  const ccy = today.base_currency;

  return (
    <div>
      <PortfolioNav portfolioId={id} navQuery={navQuery} featureStates={featureStates} />

      <div className="mt-3 flex items-baseline gap-2">
        <h1 className="text-lg font-semibold">Today</h1>
        <IntradayRefresher portfolioId={id} />
      </div>
      <p className="text-sm text-muted">
        Overnight vs intraday P&amp;L per holding — Day = Overnight (open vs prior close) + Intraday (latest vs
        open). Quotes are ~15-min delayed.
        {today.stale && today.available ? " Market closed — showing the last session (as of close)." : ""}
      </p>

      {!today.available ? (
        <div className="mt-4">
          <Empty>
            {today.reason === "feed"
              ? "The Today view needs the live market-data feed (Pro)."
              : "No intraday data yet — it populates during market hours."}
          </Empty>
        </div>
      ) : today.rows.length === 0 ? (
        <div className="mt-4">
          <Empty>No intraday-priced holdings yet{today.n_excluded ? ` (${today.n_excluded} without a quote)` : ""}.</Empty>
        </div>
      ) : (
        <>
          <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-3">
            <StatCard
              label="Overnight"
              value={gain(today.overnight_gain, ccy)}
              valueClass={signClass(today.overnight_gain ?? 0)}
              hint={pct(today.overnight_pct)}
            />
            <StatCard
              label="Intraday"
              value={gain(today.intraday_gain, ccy)}
              valueClass={signClass(today.intraday_gain ?? 0)}
              hint={pct(today.intraday_pct)}
            />
            <StatCard
              label="Day"
              value={gain(today.day_gain, ccy)}
              valueClass={signClass(today.day_gain ?? 0)}
              hint={pct(today.day_pct)}
            />
          </div>

          <Section
            title="By holding"
            note={`${today.n_priced} priced${today.n_excluded ? ` · ${today.n_excluded} without a quote` : ""}${
              today.as_of_utc ? ` · as of ${isoDate(today.as_of_utc.slice(0, 10))}` : ""
            }`}
          >
            <Table head={["Ticker", "Qty", "Prior", "Open", "Latest", "Overnight", "Intraday", "Day"]}>
              {today.rows.map((r) => (
                <tr key={r.ticker} className="border-b border-line last:border-0">
                  <td className="px-4 py-2 font-medium" title={r.label}>{r.ticker}</td>
                  <td className="px-4 py-2 text-right tabular-nums text-muted">{quantity(r.quantity)}</td>
                  <td className="px-4 py-2 text-right tabular-nums text-muted">{num(r.prev_close)}</td>
                  <td className="px-4 py-2 text-right tabular-nums text-muted">{num(r.open)}</td>
                  <td className="px-4 py-2 text-right tabular-nums">{num(r.last)}</td>
                  <td className={`px-4 py-2 text-right tabular-nums ${signClass(r.overnight_gain ?? 0)}`}>
                    {gain(r.overnight_gain, ccy)}
                    <span className="ml-1 text-[10px] text-muted/70">{pct(r.overnight_pct)}</span>
                  </td>
                  <td className={`px-4 py-2 text-right tabular-nums ${signClass(r.intraday_gain ?? 0)}`}>
                    {gain(r.intraday_gain, ccy)}
                    <span className="ml-1 text-[10px] text-muted/70">{pct(r.intraday_pct)}</span>
                  </td>
                  <td className={`px-4 py-2 text-right tabular-nums ${signClass(r.day_gain ?? 0)}`}>
                    {gain(r.day_gain, ccy)}
                    <span className="ml-1 text-[10px] text-muted/70">{pct(r.day_pct)}</span>
                  </td>
                </tr>
              ))}
            </Table>
          </Section>
        </>
      )}
    </div>
  );
}
