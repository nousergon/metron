import Link from "next/link";
import { getAccountDetail, getIntradayStatus, MetronApiError, type IntradayStatus } from "@/lib/api";
import { accountingMoneyWhole, isoDate, money, moneyWhole, quantity, signClass } from "@/lib/format";
import { Empty, Section, Table } from "@/components/ui";
import { GroupedHoldings } from "@/components/grouped-holdings";
import { LiveValuationProvider } from "@/components/live-valuation-context";
import { requireApiAuth } from "@/lib/session";

export const dynamic = "force-dynamic";

export default async function AccountPage(props: { params: Promise<{ id: string; accountId: string }> }) {
  const params = await props.params;
  const { id, accountId } = params;
  const apiAuth = await requireApiAuth();

  // Live-valuation status scoped to just this account (metron-ops#149 item 1) — mirrors
  // the Holdings page's resolution (metron-ops#153/metron#194): default LIVE whenever the
  // feed + the user's intraday toggle offer it during/just-after market hours; otherwise
  // settled. There's no `?val=` toggle on this page (unlike Holdings), so the resolution
  // is a straight availability check, no saved-view/URL override to thread through.
  const live: IntradayStatus | null = await getIntradayStatus(apiAuth, id, [accountId]).catch((): IntradayStatus | null => null);
  const liveAvailable = !!live && live.reason !== "off" && live.reason !== "feed";
  const liveOffered = liveAvailable && live.session_state !== "closed";
  const valuation: "live" | "settled" = liveOffered ? "live" : "settled";

  let detail;
  try {
    detail = await getAccountDetail(apiAuth, id, accountId, valuation);
  } catch (e) {
    if (e instanceof MetronApiError && e.status === 404) {
      return <Empty>Account not found.</Empty>;
    }
    return <Empty>Couldn&apos;t load this account. Is the backend running?</Empty>;
  }

  const { account, holdings, realized, transactions } = detail;
  const ccy = account.currency;
  const priced = holdings.some((h) => h.market_value != null);
  const liveApplied = valuation === "live" && (live?.applied ?? false);
  // Provenance-honest header (metron-ops#145/#153/#149 item 1), matching the Holdings page:
  // live mode claims intraday freshness only while the overlay actually applies; settled
  // mode names the close date it's valued at, from the data itself.
  const settledAsOf = holdings
    .map((h) => h.last_price_date)
    .filter((d): d is string => d != null)
    .sort()
    .pop();
  const holdingsNote = priced
    ? liveApplied
      ? `all values in ${ccy} · market value ~15-min delayed intraday`
      : valuation === "live"
        ? `all values in ${ccy} · session closed — market value as of last close`
        : `all values in ${ccy} · settled at ${settledAsOf ?? "last"} close`
    : "cost basis (refresh prices on the portfolio)";
  // Backend returns both oldest-first; a history reads best newest-first.
  const txns = [...transactions].reverse();
  const lots = [...realized].reverse();

  return (
    <div>
      <Link href={`/portfolios/${id}`} className="text-sm text-muted hover:text-ink">
        ← Portfolio
      </Link>

      <h1 className="mt-3 text-lg font-semibold">{account.name || account.external_id}</h1>
      <p className="text-sm text-muted">
        {account.broker} · {account.external_id} · {account.currency}
      </p>

      <Section title="Holdings" note={holdingsNote}>
        {holdings.length === 0 ? (
          <Empty>No open positions in this account.</Empty>
        ) : (
          // The provider carries the overlay state to the table's live/close provenance
          // markers (metron-ops#147/#149 item 1) — settled mode mounts it with live=false
          // so the table makes zero live claims, mirroring the Holdings page.
          <LiveValuationProvider live={liveApplied}>
            <GroupedHoldings holdings={holdings} baseCurrency={ccy} priced={priced} portfolioId={id} />
          </LiveValuationProvider>
        )}
      </Section>

      <Section title="Realized lots" note={`closed positions — FIFO; gain in ${ccy} at the close-date FX rate`}>
        {lots.length === 0 ? (
          <Empty>No closed lots in this account.</Empty>
        ) : (
          <Table head={["Ticker", "Ccy", "Opened", "Closed", "Quantity", "Proceeds", "Cost basis", "Gain", "Term"]}>
            {lots.map((r, i) => (
              <tr key={`${r.ticker}-${r.close_date}-${i}`} className="border-b border-line last:border-0">
                <td className="px-4 py-2 font-medium">{r.ticker}</td>
                <td className="px-4 py-2 text-muted">{r.currency}</td>
                <td className="px-4 py-2 text-right text-muted">{isoDate(r.open_date)}</td>
                <td className="px-4 py-2 text-right text-muted">{isoDate(r.close_date)}</td>
                <td className="px-4 py-2 text-right tabular-nums">{quantity(r.quantity)}</td>
                <td className="px-4 py-2 text-right tabular-nums">{moneyWhole(r.proceeds, r.currency)}</td>
                <td className="px-4 py-2 text-right tabular-nums">{moneyWhole(r.cost_basis, r.currency)}</td>
                <td className={`px-4 py-2 text-right font-medium tabular-nums ${signClass(r.gain_base ?? r.gain)}`}>
                  {r.gain_base != null ? (
                    accountingMoneyWhole(r.gain_base, ccy)
                  ) : (
                    <span className="text-muted" title={`No ${ccy} FX rate for ${isoDate(r.close_date)}`}>
                      {accountingMoneyWhole(r.gain, r.currency)}*
                    </span>
                  )}
                </td>
                <td className="px-4 py-2 text-right text-muted">{r.long_term ? "Long" : "Short"}</td>
              </tr>
            ))}
          </Table>
        )}
      </Section>

      <Section title="Transactions" note={`${txns.length} in this account — newest first`}>
        {txns.length === 0 ? (
          <Empty>No transactions in this account.</Empty>
        ) : (
          <Table head={["Date", "Type", "Ticker", "Ccy", "Quantity", "Price", "Amount", "Fees"]}>
            {txns.map((t, i) => (
              <tr key={`${t.trade_date}-${t.txn_type}-${t.ticker}-${i}`} className="border-b border-line last:border-0">
                <td className="px-4 py-2 font-medium tabular-nums">{isoDate(t.trade_date)}</td>
                <td className="px-4 py-2 text-right text-muted">{t.txn_type}</td>
                <td className="px-4 py-2 text-right">{t.ticker || "—"}</td>
                <td className="px-4 py-2 text-right text-muted">{t.currency}</td>
                <td className="px-4 py-2 text-right tabular-nums">{t.quantity ? quantity(t.quantity) : "—"}</td>
                <td className="px-4 py-2 text-right tabular-nums">{t.price ? money(t.price, t.currency) : "—"}</td>
                <td className="px-4 py-2 text-right tabular-nums">{moneyWhole(t.amount, t.currency)}</td>
                <td className="px-4 py-2 text-right tabular-nums text-muted">{t.fees ? money(t.fees, t.currency) : "—"}</td>
              </tr>
            ))}
          </Table>
        )}
      </Section>
    </div>
  );
}
