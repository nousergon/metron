import Link from "next/link";
import { getTearsheet, MetronApiError } from "@/lib/api";
import { isoDate, money, moneyWhole, percent, quantity, signClass, signedMoneyWhole } from "@/lib/format";
import { Empty, Section, StatCard, Table } from "@/components/ui";
import { requireTenantId } from "@/lib/session";

export const dynamic = "force-dynamic";

const PERIODS = ["1Y", "3Y", "5Y", "10Y"];

// Attractiveness component keys → human labels for the inspectable gauge breakdown
// (metron-ops#106). The count is the catalog size — the gauge note reads "N of M inputs".
const ATTRACTIVENESS_COMPONENT_LABELS: Record<string, string> = {
  valuation: "Valuation (fwd P/E vs sector)",
  upside: "Price-target upside",
  rating: "Consensus rating",
  revision: "Revision momentum",
  sentiment: "News sentiment",
};
const COMPONENT_LABELS_COUNT = Object.keys(ATTRACTIVENESS_COMPONENT_LABELS).length;

function num(v: number | null, fmt: (n: number) => string): string {
  return v != null ? fmt(v) : "—";
}

export default async function TearsheetPage({ params }: { params: { id: string; ticker: string } }) {
  const { id, ticker } = params;
  const tenantId = await requireTenantId();

  let sheet;
  try {
    sheet = await getTearsheet(tenantId, id, ticker);
  } catch (e) {
    if (e instanceof MetronApiError && e.status === 404) {
      return (
        <div>
          <Link href={`/portfolios/${id}/holdings`} className="text-sm text-muted hover:text-ink">
            ← Holdings
          </Link>
          <Empty>{decodeURIComponent(ticker).toUpperCase()} isn&apos;t a current holding.</Empty>
        </div>
      );
    }
    return <Empty>Couldn&apos;t load this tearsheet. Is the backend running?</Empty>;
  }

  const { position: p, performance: perf, technical: tech } = sheet;
  const ccy = sheet.base_currency;
  const hasHistory = perf.n_bars >= 2;

  return (
    <div>
      <Link href={`/portfolios/${id}/holdings`} className="text-sm text-muted hover:text-ink">
        ← Holdings
      </Link>

      <h1 className="mt-3 text-lg font-semibold">{sheet.ticker}</h1>
      <p className="text-sm text-muted">
        Per-holding tearsheet · values in {ccy} · as of {isoDate(sheet.as_of)}
      </p>

      {/* 1 — Position */}
      <Section title="Position">
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <StatCard label="Market value" value={num(p.market_value, (v) => moneyWhole(v, ccy))} hint={`cost ${num(p.cost_basis, (v) => moneyWhole(v, ccy))}`} />
          <StatCard
            label="Unrealized"
            value={num(p.unrealized_gain, (v) => signedMoneyWhole(v, ccy))}
            valueClass={signClass(p.unrealized_gain ?? 0)}
            hint={p.unrealized_pct != null ? percent(p.unrealized_pct) : undefined}
          />
          <StatCard label="Shares" value={quantity(p.quantity)} hint={`avg cost ${money(p.avg_cost, p.currency)}`} />
          <StatCard label="Weight" value={num(p.weight_pct, percent)} hint="of portfolio MV" />
        </div>
        <p className="mt-2 text-xs text-muted">
          {p.accounts.length > 0 ? `Held in: ${p.accounts.join(", ")}` : "—"}
          {p.currency !== ccy ? ` · native ${p.currency}` : ""}
        </p>
      </Section>

      {/* 2 — Performance */}
      <Section title="Performance" note={hasHistory ? `from price history since ${perf.history_from ? isoDate(perf.history_from) : "—"} (${perf.n_bars} bars)` : "return vs cost only — no price history cached"}>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <StatCard label="Return vs cost" value={num(perf.return_vs_cost, percent)} valueClass={signClass(perf.return_vs_cost ?? 0)} />
          <StatCard label="vs SPY" value={num(perf.vs_spy, percent)} valueClass={signClass(perf.vs_spy ?? 0)} hint="over cached window" />
          <StatCard label="Beta vs SPY" value={num(perf.beta_vs_spy, (v) => v.toFixed(2))} />
          <StatCard label="Max drawdown" value={num(perf.max_drawdown, percent)} valueClass={signClass(perf.max_drawdown ?? 0)} />
          <StatCard label="Volatility (ann.)" value={num(perf.volatility, (v) => `${(v * 100).toFixed(1)}%`)} />
          <StatCard label="Sharpe" value={num(perf.sharpe, (v) => v.toFixed(2))} valueClass={signClass(perf.sharpe ?? 0)} hint={perf.sortino != null ? `Sortino ${perf.sortino.toFixed(2)}` : undefined} />
          {PERIODS.map((k) => (
            <StatCard key={k} label={`${k} return`} value={perf.period_returns[k] != null ? percent(perf.period_returns[k]!) : "—"} valueClass={signClass(perf.period_returns[k] ?? 0)} />
          ))}
        </div>
      </Section>

      {/* 6 — Technical (price-derived) + income */}
      <Section title="Technical">
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <StatCard label="RSI (14)" value={num(tech.rsi_14, (v) => v.toFixed(0))} />
          <StatCard label="% from 52-wk high" value={num(tech.pct_from_52wk_high, percent)} valueClass={signClass(tech.pct_from_52wk_high ?? 0)} />
          <StatCard label="Forward div yield" value={num(tech.forward_div_yield, percent)} hint="fundamentals" />
        </div>
      </Section>

      {/* 3–5 — Fundamentals blocks (feed-gated). */}
      {sheet.fundamentals_available && sheet.fundamentals ? (
        (() => {
          const f = sheet.fundamentals!;
          const r2 = (v: number | null) => (v != null ? v.toFixed(2) : "—");
          const de = (v: number | null) => (v != null ? (v / 100).toFixed(2) : "—"); // yfinance D/E is a %
          const cap = (v: number | null) =>
            v == null ? "—" : v >= 1e12 ? `${(v / 1e12).toFixed(1)}T` : v >= 1e9 ? `${(v / 1e9).toFixed(1)}B` : `${(v / 1e6).toFixed(0)}M`;
          return (
            <>
              <Section title="Valuation multiples" note={[f.sector, f.industry].filter(Boolean).join(" · ") || undefined}>
                <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                  <StatCard label="Trailing P/E" value={r2(f.trailing_pe)} />
                  <StatCard label="Forward P/E" value={r2(f.forward_pe)} />
                  <StatCard label="PEG" value={r2(f.peg)} />
                  <StatCard label="EV / EBITDA" value={r2(f.ev_ebitda)} />
                  <StatCard label="Earnings growth" value={f.earnings_growth != null ? percent(f.earnings_growth) : "—"} valueClass={signClass(f.earnings_growth ?? 0)} />
                  <StatCard label="Revenue growth" value={f.revenue_growth != null ? percent(f.revenue_growth) : "—"} valueClass={signClass(f.revenue_growth ?? 0)} />
                  <StatCard label="Market cap" value={cap(f.market_cap)} />
                  <StatCard label="Beta" value={r2(f.beta)} />
                </div>
              </Section>

              <Section title="Balance sheet & profitability">
                <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                  <StatCard label="Debt / equity" value={de(f.debt_to_equity)} />
                  <StatCard label="Current ratio" value={r2(f.current_ratio)} />
                  <StatCard label="Quick ratio" value={r2(f.quick_ratio)} />
                  <StatCard label="Dividend yield" value={f.dividend_yield != null ? percent(f.dividend_yield) : "—"} />
                  <StatCard label="ROE" value={f.roe != null ? percent(f.roe) : "—"} valueClass={signClass(f.roe ?? 0)} />
                  <StatCard label="ROA" value={f.roa != null ? percent(f.roa) : "—"} valueClass={signClass(f.roa ?? 0)} />
                  <StatCard label="Gross margin" value={f.gross_margins != null ? percent(f.gross_margins) : "—"} />
                  <StatCard label="Operating margin" value={f.operating_margins != null ? percent(f.operating_margins) : "—"} />
                </div>
              </Section>

              {sheet.comps.length > 1 ? (
                <Section title="Comps" note={`same sector (${f.sector}) across your holdings`}>
                  <Table head={["Ticker", "P/E", "Fwd P/E", "EV/EBITDA", "D/E", "Div yield"]}>
                    {sheet.comps.map((c) => (
                      <tr key={c.ticker} className={`border-b border-line last:border-0 ${c.is_self ? "bg-white/5 font-medium" : ""}`}>
                        <td className="px-4 py-2">{c.ticker}{c.is_self ? " ·" : ""}</td>
                        <td className="px-4 py-2 text-right tabular-nums">{r2(c.trailing_pe)}</td>
                        <td className="px-4 py-2 text-right tabular-nums">{r2(c.forward_pe)}</td>
                        <td className="px-4 py-2 text-right tabular-nums">{r2(c.ev_ebitda)}</td>
                        <td className="px-4 py-2 text-right tabular-nums">{de(c.debt_to_equity)}</td>
                        <td className="px-4 py-2 text-right tabular-nums">{c.dividend_yield != null ? percent(c.dividend_yield) : "—"}</td>
                      </tr>
                    ))}
                  </Table>
                </Section>
              ) : null}
              {sheet.fundamentals_as_of ? (
                <p className="mt-2 text-xs text-muted">Fundamentals as of {isoDate(sheet.fundamentals_as_of)} · data-spine.</p>
              ) : null}
            </>
          );
        })()
      ) : (
        <Section title="Valuation, balance sheet & comps">
          <div className="rounded-lg border border-dashed border-line p-6 text-sm text-muted">
            Multiples (P/E, PEG, EV/EBITDA), balance-sheet ratios (D/E, current/quick, ROE/ROA, margins) and the
            same-sector comps table arrive with the Pro market-data feed. {sheet.fundamentals_reason}
          </div>
        </Section>
      )}

      {/* 7 — Composite attractiveness gauge (metron-ops#106, Phase 2). A transparent 0–100
          blend; the breakdown surfaces each component's weight + sub-score so it's never a
          black box. Feed-gated; shown only when at least one component is present. */}
      {sheet.attractiveness.available && sheet.attractiveness.score != null ? (
        (() => {
          const a = sheet.attractiveness;
          const score = a.score ?? 0;
          const scoreTone = score >= 60 ? "text-positive" : score <= 40 ? "text-negative" : "";
          const barTone = score >= 60 ? "bg-positive" : score <= 40 ? "bg-negative" : "bg-muted";
          return (
            <Section
              title="Attractiveness"
              note={`composite · ${a.coverage ?? 0} of ${COMPONENT_LABELS_COUNT} inputs`}
            >
              <div className="flex items-center gap-4">
                <div className={`text-3xl font-semibold tabular-nums ${scoreTone}`}>
                  {score.toFixed(1)}
                  <span className="ml-1 text-sm text-muted">/ 100</span>
                </div>
                <div className="h-2 flex-1 overflow-hidden rounded-full bg-line">
                  <div className={`h-full ${barTone}`} style={{ width: `${Math.max(0, Math.min(100, score))}%` }} />
                </div>
              </div>
              {/* Inspectable weighting — the deliberate "not a black box" deliverable. */}
              <div className="mt-4">
                <Table head={["Component", "Weight", "Sub-score"]}>
                  {a.components.map((c) => (
                    <tr key={c.key} className="border-b border-line last:border-0">
                      <td className="px-4 py-2">{ATTRACTIVENESS_COMPONENT_LABELS[c.key] ?? c.key}</td>
                      <td className="px-4 py-2 text-right tabular-nums">{percent(c.weight)}</td>
                      <td className="px-4 py-2 text-right tabular-nums">{c.sub_score.toFixed(2)}</td>
                    </tr>
                  ))}
                </Table>
              </div>
            </Section>
          );
        })()
      ) : null}

      {/* 8 — Consensus research + news sentiment (metron-ops#105, free sources, feed-gated). */}
      {sheet.consensus_available ? (
        (() => {
          const c = sheet.consensus;
          const ratingLabel: Record<string, string> = {
            strongBuy: "Strong Buy", buy: "Buy", hold: "Hold", sell: "Sell", strongSell: "Strong Sell",
          };
          return (
            <Section
              title="Consensus & sentiment"
              note={sheet.consensus_as_of ? `data-spine · as of ${isoDate(sheet.consensus_as_of)}` : "data-spine"}
            >
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                <StatCard
                  label="Consensus rating"
                  value={c.consensus_rating ? (ratingLabel[c.consensus_rating] ?? c.consensus_rating) : "—"}
                  hint={c.num_analysts != null ? `${c.num_analysts} analysts` : undefined}
                />
                <StatCard label="Mean target" value={num(c.price_target_mean, (v) => money(v, ccy))} hint={c.price_target_median != null ? `median ${money(c.price_target_median, ccy)}` : undefined} />
                <StatCard label="Target upside" value={num(c.price_target_upside, percent)} valueClass={signClass(c.price_target_upside ?? 0)} hint="vs live price" />
                <StatCard label="News sentiment" value={num(c.news_sentiment, (v) => v.toFixed(2))} valueClass={signClass(c.news_sentiment ?? 0)} hint={c.news_articles != null ? `${c.news_articles} articles${c.news_as_of ? ` · ${isoDate(c.news_as_of)}` : ""}` : undefined} />
                {/* Paid forward-estimate columns scaffolded now (metron-ops#107) — they resolve
                    from "N/A · paid feed" to values the moment the paid consensus feed lands,
                    with no schema/UI change here. */}
                <StatCard label="Forward EPS" value={c.estimates_available ? num(c.forward_eps, (v) => v.toFixed(2)) : c.estimates_reason} />
                <StatCard label="Consensus fwd P/E" value={c.estimates_available ? num(c.forward_pe_consensus, (v) => `${v.toFixed(1)}×`) : c.estimates_reason} />
                <StatCard label="PEG (consensus)" value={c.estimates_available ? num(c.peg_consensus, (v) => v.toFixed(2)) : c.estimates_reason} />
                <StatCard label="Estimate revisions" value={c.estimates_available ? num(c.estimate_revision_trend, percent) : c.estimates_reason} />
              </div>
            </Section>
          );
        })()
      ) : null}
    </div>
  );
}
