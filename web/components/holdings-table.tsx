"use client";

// Sortable holdings table, shared by the portfolio page and the account
// drill-down. All money columns render in the portfolio base currency; the FX
// column carries each holding's native currency and the cached rate used for
// conversion. When a foreign holding has no cached FX rate the native value is
// shown muted with a `*` (never silently treated as base currency).
//
// On a feed-entitled build the priced view adds three column bands — Valuation /
// Fundamentals / Technicals — under a grouped two-row header (Holdings metrics). The
// table gets wide, so the ticker column sticks left and a horizontal scrollbar is
// mirrored at both the top and bottom of the table. Each metric is null off-feed or on a
// coverage gap → "—" (never fabricated).

import { useEffect, useMemo, useRef, useState, useTransition, type ReactNode } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import type { Holding } from "@/lib/api";
import {
  accountingMoneyWhole,
  accountingPercent,
  decimal,
  fxRate,
  marketCapShort,
  money,
  moneyWhole,
  multiple,
  pct1,
  percent,
  quantity,
  signClass,
} from "@/lib/format";
import { setSecurityClassificationAction, setSecurityLabelAction } from "@/app/portfolios/[id]/actions";

// Canonical option lists for the inline classification override (matches the data-spine
// vocabulary: yfinance Title-Case sectors + their SPDR ETFs, plus the "Broad Market / Index"
// label used for index ETFs). Country is a curated list of common domiciles plus the
// "International" sentinel for a broad-international fund whose listing domicile (often the
// US) misrepresents its exposure (e.g. FTIHX) — it buckets as International in the geo
// split. A holding's existing value is always offered even if it's outside the list, so an
// override never drops an already-resolved value.
const SECTOR_OPTIONS = [
  "Technology",
  "Financial Services",
  "Healthcare",
  "Consumer Cyclical",
  "Consumer Defensive",
  "Energy",
  "Industrials",
  "Basic Materials",
  "Utilities",
  "Real Estate",
  "Communication Services",
  "Broad Market / Index",
];

const COUNTRY_OPTIONS = [
  "United States",
  "International",
  "Canada",
  "United Kingdom",
  "Ireland",
  "France",
  "Germany",
  "Switzerland",
  "Netherlands",
  "Italy",
  "Spain",
  "Sweden",
  "Denmark",
  "Japan",
  "China",
  "Hong Kong",
  "Taiwan",
  "South Korea",
  "Singapore",
  "India",
  "Australia",
  "Brazil",
  "Mexico",
  "Israel",
  "Uruguay",
  "Bermuda",
  "Cayman Islands",
];

type SortValue = string | number | null;

type Column = {
  key: string;
  label: string;
  pricedOnly?: boolean;
  /** Numeric columns open descending (biggest positions first). */
  defaultDesc?: boolean;
  /** Sort value — base-currency where available, native as a stable fallback. */
  value: (h: Holding) => SortValue;
};

// Position columns — kept as bespoke cells (FX fallback, inline editors, staleness). These
// drive the header + sort; the body renders them explicitly below.
const COLUMNS: Column[] = [
  { key: "ticker", label: "Ticker", value: (h) => h.ticker },
  { key: "fx", label: "FX", value: (h) => h.currency },
  { key: "quantity", label: "Quantity", defaultDesc: true, value: (h) => h.quantity },
  {
    key: "avg_cost",
    label: "Avg cost",
    defaultDesc: true,
    value: (h) => (h.fx_rate != null ? h.avg_cost * h.fx_rate : h.avg_cost),
  },
  { key: "cost_basis", label: "Cost basis", defaultDesc: true, value: (h) => h.cost_basis_base ?? h.cost_basis },
  // Reference classification (always shown, even in the cost-basis-only view). Sorted
  // ascending by default; an unclassified holding sorts last via the null-handling.
  { key: "sector", label: "Sector", value: (h) => h.sector },
  { key: "country", label: "Country", value: (h) => h.country },
  {
    key: "last",
    label: "Last",
    pricedOnly: true,
    defaultDesc: true,
    value: (h) => (h.last_price != null && h.fx_rate != null ? h.last_price * h.fx_rate : h.last_price),
  },
  {
    key: "market_value",
    label: "Market value",
    pricedOnly: true,
    defaultDesc: true,
    value: (h) => h.market_value ?? h.market_value_local,
  },
  { key: "unrealized", label: "Unrealized $", pricedOnly: true, defaultDesc: true, value: (h) => h.unrealized_gain },
  { key: "unrealized_pct", label: "Unrealized %", pricedOnly: true, defaultDesc: true, value: (h) => h.unrealized_pct },
  // Per-security period returns (metron-ops#87): Day (overnight/intraday/day, feed-gated),
  // YTD + LTM (from cached daily closes). Null → "—" (no feed / insufficient history).
  { key: "day_pct", label: "Day", pricedOnly: true, defaultDesc: true, value: (h) => h.day_pct },
  { key: "ytd_pct", label: "YTD", pricedOnly: true, defaultDesc: true, value: (h) => h.ytd_pct },
  { key: "ltm_pct", label: "LTM", pricedOnly: true, defaultDesc: true, value: (h) => h.ltm_pct },
];

export type MetricGroup = "Score" | "Valuation" | "Fundamentals" | "Balance Sheet" | "Technicals" | "Consensus";

type MetricColumn = {
  key: string;
  label: string;
  group: MetricGroup;
  value: (h: Holding) => number | null;
  /** Cell content from the non-null value (callers never see null — "—" is rendered). */
  render: (v: number, baseCurrency: string) => string;
  /** Optional text override (e.g. a categorical rating). When present it renders the cell
   *  from the holding directly; `value` still drives sort + sign-coloring. null → "—". */
  text?: (h: Holding) => string | null;
  /** Color the cell by the value's sign (growth / returns / momentum). */
  signed?: boolean;
  /** Explicit tone class from the value (overrides `signed`) — e.g. the attractiveness score
   *  bands around its 50 neutral midpoint, not around zero. */
  tone?: (v: number) => string;
  title?: string;
};

// Consensus-rating bucket → short display label (the artifact carries the camelCase key).
const RATING_LABEL: Record<string, string> = {
  strongBuy: "Strong Buy",
  buy: "Buy",
  hold: "Hold",
  sell: "Sell",
  strongSell: "Strong Sell",
};

// Valuation / Fundamentals / Technicals — declarative columns sourced from the data-spine
// fundamentals + technicals artifacts (Holdings metrics). Shown only in the priced view;
// each is null off a feed-entitled build → "—".
// Attractiveness 0–100 → tone banded around the 50 neutral midpoint (≥60 attractive,
// ≤40 unattractive). Kept here so the headline column reads at a glance.
const attractivenessTone = (v: number): string =>
  v >= 60 ? "text-positive" : v <= 40 ? "text-negative" : "";

const METRIC_COLUMNS: MetricColumn[] = [
  // ── Score (headline) — composite attractiveness (metron-ops#106, Phase 2). The first metric
  // band so it heads the priced view; a transparent 0–100 blend of the columns that follow. ──
  {
    key: "attractiveness",
    label: "Score",
    group: "Score",
    value: (h) => h.attractiveness,
    render: (v) => decimal(v, 1),
    tone: attractivenessTone,
    title:
      "Composite attractiveness (0–100): transparent blend of fwd-P/E vs sector median, " +
      "price-target upside, consensus rating, revision momentum, and news sentiment. " +
      "Open a holding's tearsheet for the weighted breakdown.",
  },
  // ── Valuation ──
  { key: "market_cap", label: "Mkt Cap", group: "Valuation", value: (h) => h.market_cap, render: (v, base) => marketCapShort(v, base) },
  { key: "pe", label: "P/E", group: "Valuation", value: (h) => h.pe, render: (v) => multiple(v) },
  { key: "fwd_pe", label: "Fwd P/E", group: "Valuation", value: (h) => h.fwd_pe, render: (v) => multiple(v) },
  { key: "pb", label: "P/B", group: "Valuation", value: (h) => h.pb, render: (v) => multiple(v) },
  { key: "ps", label: "P/S", group: "Valuation", value: (h) => h.ps, render: (v) => multiple(v) },
  { key: "ev_ebitda", label: "EV/EBITDA", group: "Valuation", value: (h) => h.ev_ebitda, render: (v) => multiple(v) },
  { key: "peg", label: "PEG", group: "Valuation", value: (h) => h.peg, render: (v) => decimal(v, 2) },
  { key: "div_yield", label: "Div Yld", group: "Valuation", value: (h) => h.div_yield, render: (v) => pct1(v) },
  // ── Fundamentals ──
  { key: "rev_growth", label: "Rev Gr", group: "Fundamentals", value: (h) => h.rev_growth, render: (v) => percent(v), signed: true },
  { key: "earnings_growth", label: "EPS Gr", group: "Fundamentals", value: (h) => h.earnings_growth, render: (v) => percent(v), signed: true },
  { key: "gross_margin", label: "Gross M", group: "Fundamentals", value: (h) => h.gross_margin, render: (v) => pct1(v) },
  { key: "op_margin", label: "Op M", group: "Fundamentals", value: (h) => h.op_margin, render: (v) => pct1(v) },
  { key: "roe", label: "ROE", group: "Fundamentals", value: (h) => h.roe, render: (v) => percent(v), signed: true },
  { key: "roa", label: "ROA", group: "Fundamentals", value: (h) => h.roa, render: (v) => percent(v), signed: true },
  { key: "beta", label: "Beta", group: "Fundamentals", value: (h) => h.beta, render: (v) => decimal(v, 2) },
  // ── Balance Sheet (absolute balances + leverage/liquidity) ──
  { key: "cash", label: "Cash", group: "Balance Sheet", value: (h) => h.cash, render: (v, base) => marketCapShort(v, base), title: "Total cash & equivalents" },
  { key: "debt", label: "Debt", group: "Balance Sheet", value: (h) => h.debt, render: (v, base) => marketCapShort(v, base), title: "Total debt" },
  { key: "net_debt", label: "Net Debt", group: "Balance Sheet", value: (h) => h.net_debt, render: (v, base) => marketCapShort(v, base), title: "Total debt − total cash (negative = net cash)" },
  { key: "debt_to_equity", label: "D/E", group: "Balance Sheet", value: (h) => h.debt_to_equity, render: (v) => decimal(v / 100, 2), title: "Debt / equity (ratio)" },
  { key: "net_debt_to_ebitda", label: "ND/EBITDA", group: "Balance Sheet", value: (h) => h.net_debt_to_ebitda, render: (v) => decimal(v, 2), title: "Net debt / EBITDA — leverage" },
  { key: "current_ratio", label: "Cur R", group: "Balance Sheet", value: (h) => h.current_ratio, render: (v) => decimal(v, 2), title: "Current ratio (liquidity)" },
  { key: "quick_ratio", label: "Quick R", group: "Balance Sheet", value: (h) => h.quick_ratio, render: (v) => decimal(v, 2), title: "Quick ratio (acid-test liquidity)" },
  { key: "fcf", label: "FCF", group: "Balance Sheet", value: (h) => h.fcf, render: (v, base) => marketCapShort(v, base), signed: true, title: "Free cash flow (TTM)" },
  // ── Technicals ──
  { key: "rsi_14", label: "RSI", group: "Technicals", value: (h) => h.rsi_14, render: (v) => decimal(v, 0), title: "Wilder RSI(14)" },
  { key: "macd_hist", label: "MACD", group: "Technicals", value: (h) => h.macd_hist, render: (v) => decimal(v, 2), signed: true, title: "MACD histogram (line − signal)" },
  { key: "pct_to_ma_50", label: "vs 50d", group: "Technicals", value: (h) => h.pct_to_ma_50, render: (v) => percent(v), signed: true, title: "% above/below the 50-day moving average" },
  { key: "pct_to_ma_200", label: "vs 200d", group: "Technicals", value: (h) => h.pct_to_ma_200, render: (v) => percent(v), signed: true, title: "% above/below the 200-day moving average" },
  { key: "pct_in_52w_range", label: "52w Rng", group: "Technicals", value: (h) => h.pct_in_52w_range, render: (v) => pct1(v), title: "Position within the 52-week low–high range" },
  { key: "mom_20d", label: "Mom 20d", group: "Technicals", value: (h) => h.mom_20d, render: (v) => percent(v), signed: true, title: "20-session price momentum" },
  // ── Consensus (research + sentiment, free sources — metron-ops#105) ──
  // Rating sorts by its signed score (strongBuy=+1 … strongSell=-1) but shows the label.
  { key: "consensus_rating", label: "Rating", group: "Consensus", value: (h) => h.consensus_score, render: () => "—", text: (h) => (h.consensus_rating ? RATING_LABEL[h.consensus_rating] ?? h.consensus_rating : null), signed: true, title: "Analyst consensus rating (free sources)" },
  { key: "price_target_mean", label: "Target", group: "Consensus", value: (h) => h.price_target_mean, render: (v, base) => money(v, base), title: "Mean analyst price target" },
  { key: "price_target_upside", label: "Upside", group: "Consensus", value: (h) => h.price_target_upside, render: (v) => percent(v), signed: true, title: "Mean target vs the live price" },
  { key: "num_analysts", label: "# An", group: "Consensus", value: (h) => h.num_analysts, render: (v) => decimal(v, 0), title: "Number of analysts behind the rating/targets" },
  { key: "news_sentiment", label: "Sentiment", group: "Consensus", value: (h) => h.news_sentiment, render: (v) => decimal(v, 2), signed: true, title: "News sentiment — trust-weighted Loughran-McDonald composite ∈ [-1, +1]" },
];

export const METRIC_GROUP_ORDER: MetricGroup[] = ["Score", "Valuation", "Fundamentals", "Balance Sheet", "Technicals", "Consensus"];

// Lookup over EVERY sortable column (position + metric), so header clicks sort uniformly.
const SORT_BY_KEY = new Map<string, (h: Holding) => SortValue>([
  ...COLUMNS.map((c) => [c.key, c.value] as const),
  ...METRIC_COLUMNS.map((c) => [c.key, c.value] as const),
]);
const DESC_BY_DEFAULT = new Set<string>([
  ...COLUMNS.filter((c) => c.defaultDesc).map((c) => c.key),
  ...METRIC_COLUMNS.map((c) => c.key), // all metrics open descending
]);

// Sticky first (ticker) column — stays put while the wide metric bands scroll right.
const STICKY = "sticky left-0 z-10";

export function HoldingsTable({
  holdings,
  baseCurrency,
  priced,
  portfolioId,
  visibleMetricGroups = METRIC_GROUP_ORDER,
  accountColumn = false,
}: {
  holdings: Holding[];
  baseCurrency: string;
  priced: boolean;
  /** When set, the Ticker cell exposes an inline alias editor (metron-ops#47). */
  portfolioId?: string;
  /** Which metric bands to render, in canonical order (column presets, metron-ops#114).
   *  Defaults to every band; the position spine is always shown. */
  visibleMetricGroups?: MetricGroup[];
  /** Render an Account column (the uncombined per-account view, metron-ops#114). The rows
   *  must carry `account_label`; sits in the position spine right after Ticker. */
  accountColumn?: boolean;
}) {
  const positionColumns = priced ? COLUMNS : COLUMNS.filter((c) => !c.pricedOnly);
  // Optional Account column injected after Ticker (the uncombined view). Kept out of the
  // shared COLUMNS so the consolidated view + its sort map are untouched.
  const headerPositionCols: { key: string; label: string }[] = accountColumn
    ? [positionColumns[0], { key: "account", label: "Account" }, ...positionColumns.slice(1)]
    : positionColumns;
  // Metric bands only in the priced view (they're feed-gated; the cost-basis-only view
  // stays exactly as before).
  const showMetrics = priced;
  // The visible bands, normalized to canonical order, with the matching metric columns.
  const visibleGroups = METRIC_GROUP_ORDER.filter((g) => visibleMetricGroups.includes(g));
  const visibleMetricCols = METRIC_COLUMNS.filter((c) => visibleGroups.includes(c.group));
  const [sort, setSort] = useState<{ key: string; desc: boolean } | null>(null);

  const sorted = useMemo(() => {
    if (!sort) return holdings;
    // The Account column isn't in the shared sort map — resolve it directly.
    const accessor = sort.key === "account" ? (h: Holding) => h.account_label : SORT_BY_KEY.get(sort.key);
    if (!accessor) return holdings;
    return [...holdings].sort((a, b) => {
      const va = accessor(a);
      const vb = accessor(b);
      if (va == null && vb == null) return 0;
      if (va == null) return 1; // nulls last in either direction
      if (vb == null) return -1;
      const cmp =
        typeof va === "string" || typeof vb === "string"
          ? String(va).localeCompare(String(vb))
          : va - (vb as number);
      return sort.desc ? -cmp : cmp;
    });
  }, [holdings, sort]);

  function toggle(key: string) {
    setSort((s) => (s?.key === key ? { key, desc: !s.desc } : { key, desc: DESC_BY_DEFAULT.has(key) }));
  }

  // Portfolio totals over the base-currency aggregates. A foreign holding with no
  // cached FX rate has no base value — it is EXCLUDED from the sum (never fabricated as
  // base), and the totals row flags `*` so the number is read as a partial total.
  const totals = useMemo(() => {
    let cost = 0;
    let mv = 0;
    let unreal = 0;
    let excluded = 0;
    for (const h of holdings) {
      const foreign = h.currency !== baseCurrency;
      const costBase = h.cost_basis_base ?? (foreign ? null : h.cost_basis);
      const mvBase = h.market_value ?? (foreign ? null : h.market_value_local);
      if (costBase != null) cost += costBase;
      else excluded += 1;
      if (priced) {
        if (mvBase != null) mv += mvBase;
        if (h.unrealized_gain != null) unreal += h.unrealized_gain;
      }
    }
    const unrealPct = priced && cost !== 0 ? unreal / cost : null;
    return { cost, mv, unreal, unrealPct, excluded };
  }, [holdings, baseCurrency, priced]);

  // Header sort-button (shared by the position + metric column headers).
  const SortTh = ({ colKey, label, title }: { colKey: string; label: string; title?: string }) => (
    <button
      type="button"
      onClick={() => toggle(colKey)}
      className="inline-flex items-center gap-1 uppercase tracking-wide hover:text-ink"
      title={title ?? `Sort by ${label}`}
    >
      {label}
      <span className={sort?.key === colKey ? "" : "invisible"}>{sort?.desc ? "▼" : "▲"}</span>
    </button>
  );

  const metricsByGroup = visibleGroups.map(
    (g) => [g, METRIC_COLUMNS.filter((c) => c.group === g)] as const,
  );

  return (
    <DualScroll deps={`${headerPositionCols.length}:${holdings.length}:${showMetrics}:${visibleMetricCols.length}`}>
      <table className="w-full text-sm">
        <thead className="sticky top-0 z-20">
          {showMetrics ? (
            // Group-band row: spans Position + each metric band.
            <tr className="border-b border-line bg-surface text-left text-[10px] uppercase tracking-wider text-muted">
              <th colSpan={headerPositionCols.length} className={`${STICKY} bg-surface px-4 py-1.5 font-semibold`}>
                Position
              </th>
              {metricsByGroup.map(([group, cols]) => (
                <th
                  key={group}
                  colSpan={cols.length}
                  className="border-l border-line px-3 py-1.5 text-center font-semibold text-accent"
                >
                  {group}
                </th>
              ))}
            </tr>
          ) : null}
          <tr className="border-b border-line bg-surface text-left text-xs uppercase tracking-wide text-muted">
            {headerPositionCols.map((col, i) => (
              <th
                key={col.key}
                className={`px-4 py-2 font-medium ${i === 0 ? `${STICKY} bg-surface` : col.key === "account" ? "text-left" : "text-right"}`}
              >
                <SortTh colKey={col.key} label={col.label} />
              </th>
            ))}
            {showMetrics
              ? metricsByGroup.map(([, cols]) =>
                  cols.map((col, j) => (
                    <th
                      key={col.key}
                      className={`px-3 py-2 text-right font-medium ${j === 0 ? "border-l border-line" : ""}`}
                    >
                      <SortTh colKey={col.key} label={col.label} title={col.title} />
                    </th>
                  )),
                )
              : null}
          </tr>
        </thead>
        <tbody>
          {sorted.map((h) => {
            const foreign = h.currency !== baseCurrency;
            // Base-currency cell with the no-fabrication fallback: muted native + `*`
            // when a foreign holding has no cached FX rate. `fmt` is money() for per-unit
            // prices (cents) or moneyWhole() for aggregates (metron-ops#45).
            const baseMoney = (
              base: number | null,
              native: number | null,
              fmt: (v: number, c: string) => string = money,
            ): ReactNode => {
              if (base != null) return fmt(base, baseCurrency);
              if (native == null) return "—";
              if (!foreign) return fmt(native, baseCurrency);
              return (
                <span className="text-muted" title={`No ${baseCurrency} FX rate cached`}>
                  {fmt(native, h.currency)}*
                </span>
              );
            };
            const avgCostBase = h.fx_rate != null ? h.avg_cost * h.fx_rate : null;
            const lastBase = h.last_price != null && h.fx_rate != null ? h.last_price * h.fx_rate : null;
            return (
              <tr key={h.ticker} className="border-b border-line last:border-0">
                <td className={`${STICKY} bg-paper px-4 py-2 font-medium`}>
                  <TickerCell h={h} portfolioId={portfolioId} />
                </td>
                {accountColumn ? (
                  <td className="px-4 py-2 text-left text-muted">{h.account_label ?? "—"}</td>
                ) : null}
                <td className="px-4 py-2 text-right text-muted">
                  {foreign ? `${h.currency} @ ${h.fx_rate != null ? fxRate(h.fx_rate) : "—"}` : h.currency}
                </td>
                <td className="px-4 py-2 text-right tabular-nums">{quantity(h.quantity)}</td>
                <td className="px-4 py-2 text-right tabular-nums">{baseMoney(avgCostBase, h.avg_cost)}</td>
                <td className="px-4 py-2 text-right tabular-nums">
                  {baseMoney(h.cost_basis_base, h.cost_basis, moneyWhole)}
                </td>
                <ClassifyCell h={h} field="sector" portfolioId={portfolioId} />
                <ClassifyCell h={h} field="country" portfolioId={portfolioId} />
                {priced ? (
                  <>
                    <td
                      className={`px-4 py-2 text-right tabular-nums ${h.last_price_stale ? "text-amber-500" : "text-muted"}`}
                      title={
                        h.last_price_stale && h.last_price_date
                          ? `Stale — last close ${h.last_price_date}; the market-data feed hasn’t updated since`
                          : undefined
                      }
                    >
                      {baseMoney(lastBase, h.last_price)}
                      {h.last_price_stale ? <span className="ml-0.5" aria-hidden>⚠</span> : null}
                    </td>
                    <td className="px-4 py-2 text-right tabular-nums">
                      {baseMoney(h.market_value, h.market_value_local, moneyWhole)}
                    </td>
                    <td className={`px-4 py-2 text-right tabular-nums ${signClass(h.unrealized_gain ?? 0)}`}>
                      {h.unrealized_gain != null ? accountingMoneyWhole(h.unrealized_gain, baseCurrency) : "—"}
                    </td>
                    <td className={`px-4 py-2 text-right tabular-nums ${signClass(h.unrealized_pct ?? 0)}`}>
                      {h.unrealized_pct != null ? accountingPercent(h.unrealized_pct) : "—"}
                    </td>
                    <td
                      className={`px-4 py-2 text-right tabular-nums ${h.day_pct != null ? signClass(h.day_pct) : "text-muted"}`}
                      title={
                        h.overnight_pct != null && h.intraday_pct != null
                          ? `overnight ${accountingPercent(h.overnight_pct)} · intraday ${accountingPercent(h.intraday_pct)}`
                          : undefined
                      }
                    >
                      {h.day_pct != null ? accountingPercent(h.day_pct) : "—"}
                    </td>
                    <td className={`px-4 py-2 text-right tabular-nums ${h.ytd_pct != null ? signClass(h.ytd_pct) : "text-muted"}`}>
                      {h.ytd_pct != null ? accountingPercent(h.ytd_pct) : "—"}
                    </td>
                    <td className={`px-4 py-2 text-right tabular-nums ${h.ltm_pct != null ? signClass(h.ltm_pct) : "text-muted"}`}>
                      {h.ltm_pct != null ? accountingPercent(h.ltm_pct) : "—"}
                    </td>
                  </>
                ) : null}
                {showMetrics
                  ? metricsByGroup.map(([, cols]) =>
                      cols.map((col, j) => {
                        const v = col.value(h);
                        const tone =
                          v == null
                            ? "text-muted"
                            : col.tone
                              ? col.tone(v)
                              : col.signed
                                ? signClass(v)
                                : "";
                        // Categorical columns (e.g. the consensus rating) render their own
                        // label; numeric columns render from the non-null value.
                        const cell = col.text ? col.text(h) : v == null ? null : col.render(v, baseCurrency);
                        return (
                          <td
                            key={col.key}
                            className={`px-3 py-2 text-right tabular-nums ${j === 0 ? "border-l border-line" : ""} ${cell == null ? "text-muted" : tone}`}
                            title={col.title}
                          >
                            {cell == null ? "—" : cell}
                          </td>
                        );
                      }),
                    )
                  : null}
              </tr>
            );
          })}
        </tbody>
        {holdings.length > 0 ? (
          <tfoot>
            <tr className="border-t border-line bg-surface font-medium">
              <td
                className={`${STICKY} bg-surface px-4 py-2`}
                title={totals.excluded > 0 ? `${totals.excluded} holding(s) excluded — no ${baseCurrency} FX rate cached` : undefined}
              >
                Total{totals.excluded > 0 ? "*" : ""}
              </td>
              {accountColumn ? <td className="px-4 py-2" /> : null}
              <td className="px-4 py-2" />
              <td className="px-4 py-2" />
              <td className="px-4 py-2" />
              <td className="px-4 py-2 text-right tabular-nums">{moneyWhole(totals.cost, baseCurrency)}</td>
              {/* Sector / Country are per-security labels — no portfolio total. */}
              <td className="px-4 py-2" />
              <td className="px-4 py-2" />
              {priced ? (
                <>
                  <td className="px-4 py-2" />
                  <td className="px-4 py-2 text-right tabular-nums">{moneyWhole(totals.mv, baseCurrency)}</td>
                  <td className={`px-4 py-2 text-right tabular-nums ${signClass(totals.unreal)}`}>
                    {accountingMoneyWhole(totals.unreal, baseCurrency)}
                  </td>
                  <td className={`px-4 py-2 text-right tabular-nums ${signClass(totals.unrealPct ?? 0)}`}>
                    {totals.unrealPct != null ? accountingPercent(totals.unrealPct) : "—"}
                  </td>
                  {/* Day / YTD / LTM are per-security returns — no meaningful portfolio total. */}
                  <td className="px-4 py-2" />
                  <td className="px-4 py-2" />
                  <td className="px-4 py-2" />
                </>
              ) : null}
              {/* Metric columns are per-security — no portfolio total. */}
              {showMetrics ? visibleMetricCols.map((c) => <td key={c.key} className="px-3 py-2" />) : null}
            </tr>
          </tfoot>
        ) : null}
      </table>
    </DualScroll>
  );
}

/** Wraps a wide table in a single horizontal scroll container and mirrors its scrollbar at
 *  the TOP as well, so a long table can be scrolled horizontally from either end without
 *  hunting for the bottom bar. The two bars stay in sync; the top bar hides when the table
 *  fits (no overflow). `deps` forces a re-measure when the column/row set changes. */
function DualScroll({ children, deps }: { children: ReactNode; deps: string }) {
  const bodyRef = useRef<HTMLDivElement>(null);
  const topRef = useRef<HTMLDivElement>(null);
  const [scrollW, setScrollW] = useState(0);
  const [clientW, setClientW] = useState(0);

  useEffect(() => {
    const el = bodyRef.current;
    if (!el) return;
    const measure = () => {
      setScrollW(el.scrollWidth);
      setClientW(el.clientWidth);
    };
    measure();
    // ResizeObserver is absent in some test/SSR environments — the initial measure above
    // still wires the mirror bar; we only skip live re-measure on resize.
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [deps]);

  const overflows = scrollW > clientW + 1;
  const syncFromTop = () => {
    if (bodyRef.current && topRef.current) bodyRef.current.scrollLeft = topRef.current.scrollLeft;
  };
  const syncFromBody = () => {
    if (bodyRef.current && topRef.current) topRef.current.scrollLeft = bodyRef.current.scrollLeft;
  };

  return (
    <div>
      {/* Top mirror scrollbar — only when the table actually overflows horizontally. */}
      <div
        ref={topRef}
        onScroll={syncFromTop}
        className={`overflow-x-auto ${overflows ? "" : "hidden"}`}
        aria-hidden
      >
        <div style={{ width: scrollW, height: 1 }} />
      </div>
      <div ref={bodyRef} onScroll={syncFromBody} className="overflow-x-auto rounded-lg border border-line">
        {children}
      </div>
    </div>
  );
}

/** The Sector / Country cell. With a portfolioId it's editable: an UNCLASSIFIED holding
 *  shows a "Set …" dropdown directly (the gap the user wants to fill); a classified one
 *  shows the value with a small ✎ to correct it. Choosing the blank option clears the
 *  override (reverts to the spine-resolved value). Read-only contexts just render the
 *  value. The override is tenant-scoped (it never mutates the shared reference row). */
function ClassifyCell({
  h,
  field,
  portfolioId,
}: {
  h: Holding;
  field: "sector" | "country";
  portfolioId?: string;
}) {
  const router = useRouter();
  const value = field === "sector" ? h.sector : h.country;
  const [editing, setEditing] = useState(false);
  const [pending, start] = useTransition();
  const [error, setError] = useState<string | null>(null);

  // Read-only context (no portfolioId) — just the value.
  if (!portfolioId) {
    return <td className="px-4 py-2 text-right text-muted">{value ?? "—"}</td>;
  }

  const base = field === "sector" ? SECTOR_OPTIONS : COUNTRY_OPTIONS;
  // Always offer the current value, even if it's outside the curated list, so an override
  // never drops an already-resolved (possibly non-canonical) value.
  const options = value && !base.includes(value) ? [value, ...base] : base;

  function choose(next: string) {
    setError(null);
    start(async () => {
      const r = await setSecurityClassificationAction(portfolioId!, h.ticker, field, next);
      if (!r.ok) {
        setError(r.message);
        return;
      }
      setEditing(false);
      router.refresh();
    });
  }

  // Show the dropdown directly when unclassified (the gap to fill), or when the user
  // clicked ✎ to correct an existing value.
  const showSelect = value == null || editing;
  if (showSelect) {
    return (
      <td className="px-4 py-2 text-right">
        <select
          value={value ?? ""}
          disabled={pending}
          onChange={(e) => choose(e.target.value)}
          aria-label={`Set ${field} for ${h.ticker}`}
          className="max-w-[10rem] rounded border border-line bg-surface px-1.5 py-0.5 text-xs"
        >
          <option value="">{value == null ? `Set ${field}…` : `— clear ${field} —`}</option>
          {options.map((o) => (
            <option key={o} value={o}>
              {o}
            </option>
          ))}
        </select>
        {error ? <div className="mt-0.5 text-[10px] text-negative">{error}</div> : null}
      </td>
    );
  }

  return (
    <td className="px-4 py-2 text-right text-muted">
      <span className="inline-flex items-center gap-1">
        {value}
        <button
          type="button"
          onClick={() => setEditing(true)}
          aria-label={`Edit ${field} for ${h.ticker}`}
          title={`Edit ${field}`}
          className="text-xs text-muted/70 transition hover:text-ink"
        >
          ✎
        </button>
      </span>
    </td>
  );
}

/** The Ticker cell: shows a user alias (with the raw symbol beneath) when set, else the
 *  symbol. With a portfolioId it exposes an inline editor so an opaque numeric-CUSIP bond
 *  can be named (metron-ops#47). Read-only contexts (no portfolioId) just render. */
function TickerCell({ h, portfolioId }: { h: Holding; portfolioId?: string }) {
  const router = useRouter();
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(h.user_label ?? "");
  const [error, setError] = useState<string | null>(null);
  const [pending, start] = useTransition();

  if (!portfolioId) {
    // Read-only: alias (if any) over the symbol.
    return h.user_label ? (
      <span>
        {h.user_label}
        <span className="ml-1 text-xs font-normal text-muted">{h.ticker}</span>
      </span>
    ) : (
      <span>{h.ticker}</span>
    );
  }

  function save() {
    setError(null);
    start(async () => {
      const r = await setSecurityLabelAction(portfolioId!, h.ticker, value);
      if (!r.ok) {
        setError(r.message);
        return;
      }
      setEditing(false);
      router.refresh();
    });
  }

  if (editing) {
    return (
      <span className="inline-flex items-center gap-1">
        <input
          autoFocus
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") save();
            if (e.key === "Escape") {
              setValue(h.user_label ?? "");
              setEditing(false);
            }
          }}
          placeholder={h.ticker}
          aria-label={`Label for ${h.ticker}`}
          disabled={pending}
          className="w-32 rounded border border-line bg-surface px-1.5 py-0.5 text-sm font-normal"
        />
        <button type="button" onClick={save} disabled={pending} className="text-xs text-accent hover:underline">
          Save
        </button>
        {error ? <span className="text-xs text-negative">{error}</span> : null}
      </span>
    );
  }

  // A numeric / CUSIP-style ticker (bonds, CDs, treasuries) is unreadable without a
  // name, so prompt for one explicitly when there's no label yet (metron-ops#57).
  const numericish = /^\d/.test(h.ticker);
  const startEdit = () => {
    setValue(h.user_label ?? "");
    setEditing(true);
  };

  return (
    <span className="inline-flex items-baseline gap-1">
      {h.user_label ? (
        <>
          <span>{h.user_label}</span>
          <Link href={`/portfolios/${portfolioId}/tearsheet/${encodeURIComponent(h.ticker)}`} className="text-xs font-normal text-muted hover:text-ink hover:underline" title="Open tearsheet">
            {h.ticker}
          </Link>
        </>
      ) : (
        <Link href={`/portfolios/${portfolioId}/tearsheet/${encodeURIComponent(h.ticker)}`} className="hover:underline" title="Open tearsheet">
          {h.ticker}
        </Link>
      )}
      {!h.user_label && numericish ? (
        <button
          type="button"
          onClick={startEdit}
          aria-label={`Add label for ${h.ticker}`}
          className="rounded border border-line px-1 text-[10px] font-normal uppercase tracking-wide text-accent hover:bg-white/5"
        >
          + name
        </button>
      ) : (
        <button
          type="button"
          onClick={startEdit}
          aria-label={`${h.user_label ? "Edit" : "Add"} label for ${h.ticker}`}
          title={h.user_label ? "Edit label" : "Add a label/alias"}
          className="text-xs font-normal text-muted transition hover:text-ink"
        >
          ✎
        </button>
      )}
    </span>
  );
}
