import {
  acctParams,
  getIncome,
  getRealized,
  getSummary,
  getTax,
  getTaxPlanning,
  getTransactions,
  MetronApiError,
  type TaxPlanning,
} from "@/lib/api";
import { accountingMoney, accountingMoneyWhole, isoDate, money, moneyWhole, percent, quantity, signClass, signedMoneyWhole } from "@/lib/format";
import { Empty, Section, StatCard, Table } from "@/components/ui";
import { PortfolioNav } from "@/components/portfolio-nav";
import { navFeatureStates } from "@/lib/entitlements";
import { requireTenantId } from "@/lib/session";
import { resolveAccountIds } from "@/lib/selection";

export const dynamic = "force-dynamic";

export default async function TaxPage({
  params,
  searchParams,
}: {
  params: { id: string };
  searchParams: { account_id?: string | string[] };
}) {
  const { id } = params;
  const tenantId = await requireTenantId();
  const featureStates = await navFeatureStates(tenantId);

  // URL selection wins; with none, the saved panel selection is applied (redirect).
  const accountIds = await resolveAccountIds(tenantId, id, `/portfolios/${id}/tax`, searchParams.account_id);
  const navQuery = acctParams(accountIds);

  let taxData, summary, income, transactions, realized;
  try {
    [taxData, summary, income, transactions, realized] = await Promise.all([
      getTax(tenantId, id, accountIds),
      getSummary(tenantId, id, accountIds),
      getIncome(tenantId, id, accountIds, true), // taxable accounts only
      getTransactions(tenantId, id, accountIds, true),
      getRealized(tenantId, id, accountIds, true),
    ]);
  } catch (e) {
    if (e instanceof MetronApiError && e.status === 404) {
      return <Empty>Portfolio not found.</Empty>;
    }
    return <Empty>Couldn&apos;t load tax. Is the backend running?</Empty>;
  }

  // Fetched separately fail-soft: a planning hiccup must never blank the tax page.
  let planning: TaxPlanning | null = null;
  try {
    planning = await getTaxPlanning(tenantId);
  } catch {
    planning = null;
  }

  const ccy = summary.base_currency;
  const currentYear = new Date().getFullYear();
  // Only widen the income table with a Distributions column when there actually are
  // tax-deferred withdrawals — most taxable-only users won't have any.
  const hasDistributions = income.some((y) => y.distributions !== 0);
  // All-time totals across every year — surfaced as a footer row so lifetime realized
  // isn't buried under the current-year "YTD" line (metron-ops#75: a prior-year gain like
  // a 2025 sale reads as "low" when only the YTD row is glanced at).
  const totals = income.reduce(
    (a, y) => ({
      realized_st: a.realized_st + y.realized_st,
      realized_lt: a.realized_lt + y.realized_lt,
      dividends: a.dividends + y.dividends,
      interest: a.interest + y.interest,
      distributions: a.distributions + y.distributions,
      taxable_income: a.taxable_income + y.taxable_income,
    }),
    { realized_st: 0, realized_lt: 0, dividends: 0, interest: 0, distributions: 0, taxable_income: 0 },
  );
  // History reads best newest-first (backend returns oldest-first).
  const lots = [...realized].reverse();
  const txns = [...transactions].reverse();

  // Unrealized: lead with the AUTHORITATIVE position-level total (reconciles to the
  // Accounts table); ST/LT below are the lot-classified slice. The gap is positions whose
  // broker history starts mid-position — counted in the total, not assignable to a term.
  const lotTotal = taxData.unrealized_total;
  const total = taxData.unrealized_position_total ?? lotTotal;
  const priced = total != null;
  const gap = total != null && lotTotal != null ? total - lotTotal : null;
  const hasGap = taxData.n_incomplete > 0 || (gap != null && Math.abs(gap) >= 1);

  return (
    <div>
      <PortfolioNav portfolioId={id} navQuery={navQuery} featureStates={featureStates} />

      <h1 className="mt-3 text-lg font-semibold">Tax</h1>
      <p className="text-sm text-muted">
        Realized income by year, plus per-lot holding-period term and unrealized P&amp;L (at the last close, in {ccy})
        with harvestable losses flagged. Taxable accounts only. Descriptive, not advice.
      </p>
      {taxData.n_accounts_excluded > 0 ? (
        <p className="mt-1 text-xs text-muted">
          {taxData.n_accounts_excluded} tax-advantaged account{taxData.n_accounts_excluded === 1 ? "" : "s"} (IRA /
          401(k) / Roth …) excluded — gains there are never taxed.
        </p>
      ) : null}

      {planning !== null && planning.available ? (
        <Section
          title="Tax planning"
          note={
            planning.projection
              ? `TY${planning.projection.tax_year} projection · as of ${isoDate(planning.projection.as_of)} · ${planning.projection.pack_status} parameters`
              : "year-round projection from the telos engine"
          }
        >
          {planning.schema_error ? (
            <Empty>{planning.schema_error}</Empty>
          ) : !planning.projection ? (
            <Empty>
              No tax projection yet — run <code>python -m telos.planning</code> against your income
              scenario and sync the artifact to the server.
            </Empty>
          ) : (
            <>
              <div
                className={`mb-3 rounded-md border px-4 py-3 text-sm ${
                  planning.projection.headline.payment_recommended
                    ? "border-negative/40 bg-negative/5"
                    : "border-line bg-surface"
                }`}
              >
                <span className="font-medium">
                  {planning.projection.headline.payment_recommended
                    ? `Estimated payment recommended: ${moneyWhole(Number(planning.projection.headline.recommended_amount), ccy)}`
                    : "No estimated payment needed right now"}
                </span>
                {planning.projection.headline.next_due_date ? (
                  <span className="text-muted"> · next due {isoDate(planning.projection.headline.next_due_date)}</span>
                ) : null}
                <p className="mt-1 text-xs text-muted">{planning.projection.headline.message}</p>
              </div>
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                <StatCard
                  label="Projected total tax"
                  value={moneyWhole(Number(planning.projection.projected.total_tax), ccy)}
                  hint={`on ${moneyWhole(Number(planning.projection.projected.agi), ccy)} AGI`}
                />
                <StatCard
                  label="Effective / marginal rate"
                  value={`${percent(Number(planning.projection.projected.effective_rate_on_agi))} / ${percent(Number(planning.projection.projected.marginal_ordinary_rate))}`}
                  hint="on AGI / ordinary bracket"
                />
                <StatCard
                  label="Safe harbor required"
                  value={moneyWhole(Number(planning.projection.safe_harbor.required_annual_payment), ccy)}
                  hint={planning.projection.safe_harbor.basis.replaceAll("_", " ")}
                />
                <StatCard
                  label="Balance due at filing"
                  value={accountingMoneyWhole(Number(planning.projection.projected.balance_due), ccy)}
                  valueClass={signClass(-Number(planning.projection.projected.balance_due))}
                  hint="after withholding + payments"
                />
              </div>
              {planning.projection.quarters.length > 0 ? (
                <div className="mt-3">
                  <Table head={["Installment", "Due", "Required", "Paid", "Shortfall", "Status"]}>
                    {planning.projection.quarters.map((q) => (
                      <tr key={q.quarter} className="border-b border-line last:border-0">
                        <td className="px-4 py-2 font-medium">Q{q.quarter}</td>
                        <td className="px-4 py-2 text-right text-muted">{isoDate(q.due_date)}</td>
                        <td className="px-4 py-2 text-right tabular-nums">{moneyWhole(Number(q.required), ccy)}</td>
                        <td className="px-4 py-2 text-right tabular-nums">{moneyWhole(Number(q.paid), ccy)}</td>
                        <td className={`px-4 py-2 text-right tabular-nums ${Number(q.shortfall) > 0 ? "text-negative" : ""}`}>
                          {Number(q.shortfall) > 0 ? moneyWhole(Number(q.shortfall), ccy) : "—"}
                        </td>
                        <td className="px-4 py-2 text-right">
                          <span
                            className={`text-xs uppercase tracking-wide ${
                              q.status === "overdue"
                                ? "font-medium text-negative"
                                : q.status === "paid"
                                  ? "text-muted"
                                  : ""
                            }`}
                          >
                            {q.status}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </Table>
                </div>
              ) : (
                <p className="mt-2 text-xs text-muted">
                  Withholding meets the required annual payment — no 1040-ES installments due.
                </p>
              )}
              <p className="mt-2 text-xs text-muted">
                Deterministic projection from the telos engine ({planning.projection.filing_status.replaceAll("_", " ")},
                withholding {moneyWhole(Number(planning.projection.projected.total_withholding), ccy)}, estimated payments
                made {moneyWhole(Number(planning.projection.projected.estimated_payments_made), ccy)}). §6654 safe-harbor
                math; statutory due dates. Descriptive, not advice.
              </p>
            </>
          )}
        </Section>
      ) : null}

      <Section
        title="Realized income by year"
        note={
          hasDistributions
            ? "short/long-term gains, dividends, interest + tax-deferred distributions"
            : "taxable accounts only — short/long-term gains, dividends, interest"
        }
      >
        {income.length === 0 ? (
          <Empty>No taxable realized income yet.</Empty>
        ) : (
          <Table
            head={[
              "Year",
              "Short-term",
              "Long-term",
              "Dividends",
              "Interest",
              ...(hasDistributions ? ["Distributions"] : []),
              "Taxable income",
            ]}
          >
            {income.map((y) => (
              <tr key={y.year} className="border-b border-line last:border-0">
                <td className="px-4 py-2 font-medium">
                  {y.year}
                  {y.year === currentYear ? <span className="ml-1 text-[10px] uppercase tracking-wide text-muted">YTD</span> : null}
                </td>
                <td className={`px-4 py-2 text-right tabular-nums ${signClass(y.realized_st)}`}>
                  {accountingMoneyWhole(y.realized_st, ccy)}
                </td>
                <td className={`px-4 py-2 text-right tabular-nums ${signClass(y.realized_lt)}`}>
                  {accountingMoneyWhole(y.realized_lt, ccy)}
                </td>
                <td className="px-4 py-2 text-right tabular-nums">{moneyWhole(y.dividends, ccy)}</td>
                <td className="px-4 py-2 text-right tabular-nums">{moneyWhole(y.interest, ccy)}</td>
                {hasDistributions ? (
                  <td className="px-4 py-2 text-right tabular-nums">{moneyWhole(y.distributions, ccy)}</td>
                ) : null}
                <td className="px-4 py-2 text-right font-medium tabular-nums">{moneyWhole(y.taxable_income, ccy)}</td>
              </tr>
            ))}
            {income.length > 1 ? (
              <tr className="border-t-2 border-line bg-surface font-medium">
                <td className="px-4 py-2 uppercase tracking-wide text-muted">All-time</td>
                <td className={`px-4 py-2 text-right tabular-nums ${signClass(totals.realized_st)}`}>
                  {accountingMoneyWhole(totals.realized_st, ccy)}
                </td>
                <td className={`px-4 py-2 text-right tabular-nums ${signClass(totals.realized_lt)}`}>
                  {accountingMoneyWhole(totals.realized_lt, ccy)}
                </td>
                <td className="px-4 py-2 text-right tabular-nums">{moneyWhole(totals.dividends, ccy)}</td>
                <td className="px-4 py-2 text-right tabular-nums">{moneyWhole(totals.interest, ccy)}</td>
                {hasDistributions ? (
                  <td className="px-4 py-2 text-right tabular-nums">{moneyWhole(totals.distributions, ccy)}</td>
                ) : null}
                <td className="px-4 py-2 text-right tabular-nums">{moneyWhole(totals.taxable_income, ccy)}</td>
              </tr>
            ) : null}
          </Table>
        )}
        <p className="mt-2 text-xs text-muted">
          {income.some((y) => y.year === currentYear)
            ? `${currentYear} is year-to-date — partial through ${isoDate(taxData.as_of)}.`
            : "Prior full years; the current year shows once it has taxable activity."}
          {hasDistributions
            ? " Distributions are withdrawals from tax-deferred accounts (Trad IRA / 401(k), incl. RMDs) — taxable ordinary income even though those accounts' internal gains aren't taxed."
            : ""}
        </p>
      </Section>

      {priced ? (
        <Section title="Unrealized" note="market-relative to cost basis, taxable accounts">
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <StatCard
              label="Total unrealized"
              value={accountingMoney(total as number, ccy)}
              valueClass={signClass(total as number)}
              hint={hasGap ? "all taxable positions" : "vs cost basis"}
            />
            <StatCard
              label="Lot-classified (short-term)"
              value={accountingMoney(taxData.unrealized_st ?? 0, ccy)}
              valueClass={signClass(taxData.unrealized_st ?? 0)}
            />
            <StatCard
              label="Lot-classified (long-term)"
              value={accountingMoney(taxData.unrealized_lt ?? 0, ccy)}
              valueClass={signClass(taxData.unrealized_lt ?? 0)}
            />
            <StatCard
              label="Harvestable loss"
              value={money(taxData.harvestable_loss ?? 0, ccy)}
              hint="available to harvest"
            />
          </div>
          {hasGap ? (
            <p className="mt-2 text-xs text-muted">
              Total reflects every taxable position. {signedMoneyWhole(lotTotal ?? 0, ccy)} is lot-classified below
              (term + harvesting);{" "}
              {gap != null ? signedMoneyWhole(gap, ccy) : "the remainder"} sits in {taxData.n_incomplete} position
              {taxData.n_incomplete === 1 ? "" : "s"} whose broker history starts mid-position
              {taxData.incomplete_tickers.length > 0 ? ` (${taxData.incomplete_tickers.join(", ")})` : ""} — counted in
              the total but not assignable to a holding-period term until the opening trades are imported.
            </p>
          ) : null}
        </Section>
      ) : (
        <div className="mt-4">
          <Empty>Refresh prices on the portfolio page to value lots and surface harvestable losses.</Empty>
        </div>
      )}

      <Section title="Open lots" note={`${taxData.n_lots} open · cost basis & term are price-free`}>
        {taxData.lots.length === 0 ? (
          <Empty>No open lots.</Empty>
        ) : (
          <Table head={["Ticker", "Ccy", "Opened", "Term", "Quantity", "Cost basis", "Market value", "Unrealized", "Harvest"]}>
            {taxData.lots.map((l, i) => (
              <tr key={`${l.ticker}-${l.open_date}-${i}`} className="border-b border-line last:border-0">
                <td className="px-4 py-2 font-medium">{l.ticker}</td>
                <td className="px-4 py-2 text-muted">{l.currency}</td>
                <td className="px-4 py-2 text-right text-muted">{isoDate(l.open_date)}</td>
                <td className="px-4 py-2 text-right text-muted">{l.term === "Long-term" ? "LT" : l.term === "Short-term" ? "ST" : "?"}</td>
                <td className="px-4 py-2 text-right tabular-nums">{quantity(l.quantity)}</td>
                <td className="px-4 py-2 text-right tabular-nums">{money(l.cost_basis, l.currency)}</td>
                <td className="px-4 py-2 text-right tabular-nums">{l.market_value != null ? money(l.market_value, ccy) : "—"}</td>
                <td className={`px-4 py-2 text-right tabular-nums ${signClass(l.unrealized_gain ?? 0)}`}>
                  {l.unrealized_gain != null ? accountingMoney(l.unrealized_gain, ccy) : "—"}
                </td>
                <td className="px-4 py-2 text-right tabular-nums">
                  {(l.harvestable_loss ?? 0) > 0 ? (
                    <span className="text-negative">{money(l.harvestable_loss as number, ccy)}</span>
                  ) : (
                    "—"
                  )}
                </td>
              </tr>
            ))}
          </Table>
        )}
      </Section>

      <Section title="Realized lots" note={`closed positions — FIFO; gain in ${ccy} at the close-date FX rate`}>
        {lots.length === 0 ? (
          <Empty>No closed lots yet.</Empty>
        ) : (
          <Table head={["Ticker", "Ccy", "Opened", "Closed", "Quantity", "Proceeds", "Cost basis", "Gain", "Term"]}>
            {lots.map((r, i) => (
              <tr key={`${r.ticker}-${r.close_date}-${i}`} className="border-b border-line last:border-0">
                <td className="px-4 py-2 font-medium">{r.ticker}</td>
                <td className="px-4 py-2 text-muted">{r.currency}</td>
                <td className="px-4 py-2 text-right text-muted">{isoDate(r.open_date)}</td>
                <td className="px-4 py-2 text-right text-muted">{isoDate(r.close_date)}</td>
                <td className="px-4 py-2 text-right tabular-nums">{quantity(r.quantity)}</td>
                <td className="px-4 py-2 text-right tabular-nums">{money(r.proceeds, r.currency)}</td>
                <td className="px-4 py-2 text-right tabular-nums">{money(r.cost_basis, r.currency)}</td>
                <td className={`px-4 py-2 text-right font-medium tabular-nums ${signClass(r.gain_base ?? r.gain)}`}>
                  {r.gain_base != null ? (
                    accountingMoney(r.gain_base, ccy)
                  ) : (
                    <span className="text-muted" title={`No ${ccy} FX rate for ${isoDate(r.close_date)}`}>
                      {accountingMoney(r.gain, r.currency)}*
                    </span>
                  )}
                </td>
                <td className="px-4 py-2 text-right text-muted">{r.long_term ? "Long" : "Short"}</td>
              </tr>
            ))}
          </Table>
        )}
      </Section>

      <Section title="Transactions" note={`${txns.length} imported — newest first`}>
        {txns.length === 0 ? (
          <Empty>No transactions imported yet.</Empty>
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
                <td className="px-4 py-2 text-right tabular-nums">{money(t.amount, t.currency)}</td>
                <td className="px-4 py-2 text-right tabular-nums text-muted">{t.fees ? money(t.fees, t.currency) : "—"}</td>
              </tr>
            ))}
          </Table>
        )}
      </Section>
    </div>
  );
}
