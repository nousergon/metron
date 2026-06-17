import Link from "next/link";
import { getTearsheet, MetronApiError } from "@/lib/api";
import { isoDate, money, moneyWhole, percent, quantity, signClass, signedMoneyWhole } from "@/lib/format";
import { Empty, Section, StatCard } from "@/components/ui";
import { requireTenantId } from "@/lib/session";

export const dynamic = "force-dynamic";

const PERIODS = ["1Y", "3Y", "5Y", "10Y"];

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

      {/* 3–5 — Fundamentals-gated blocks: honest N/A until the spine artifact ships. */}
      <Section title="Valuation, balance sheet & comps">
        <div className="rounded-lg border border-dashed border-line p-6 text-sm text-muted">
          {sheet.fundamentals_available ? (
            "Fundamentals available."
          ) : (
            <>
              Multiples (P/E, PEG, EV/EBITDA), balance-sheet ratios (D/E, current/quick, ROE/ROA, margins) and the
              same-sector comps table aren&apos;t available yet. {sheet.fundamentals_reason}
            </>
          )}
        </div>
      </Section>
    </div>
  );
}
