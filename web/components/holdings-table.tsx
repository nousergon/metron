"use client";

// Sortable holdings table, shared by the portfolio page and the account
// drill-down. All money columns render in the portfolio base currency; the FX
// column carries each holding's native currency and the cached rate used for
// conversion. When a foreign holding has no cached FX rate the native value is
// shown muted with a `*` (never silently treated as base currency).
//
// COLUMN MODEL (metron-ops#118+, realigned metron-ops#140): the always-on frozen spine is
// Ticker + Market Value (priced views) — every other column belongs to a toggleable BAND, so
// no analytic preset needs to drag the full Position/Value economics along just for anchoring
// context. Fundamentals covers the whole financial-statement picture — growth/margins/returns
// AND balance-sheet leverage/liquidity — in one band, matching how institutional platforms
// (Bloomberg, FactSet) group it, rather than splitting balance-sheet health out as an
// unrelated axis. Bands render under a grouped two-row header; each metric is null off-feed
// or on a coverage gap → "—" (never fabricated).

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

// Instrument-type override options (metron-ops#115). Value = the security_type key the
// classifier emits + the API validates; label = the friendly display. Order mirrors the
// "By asset class" grouping.
const TYPE_OPTIONS: { value: string; label: string }[] = [
  { value: "equity", label: "Equity" },
  { value: "etf", label: "ETF" },
  { value: "fund", label: "Fund" },
  { value: "treasury", label: "Treasury" },
  { value: "bond", label: "Bond" },
  { value: "cd", label: "CD" },
  { value: "cash", label: "Cash" },
  { value: "option", label: "Option" },
  { value: "other", label: "Other" },
];
const TYPE_LABEL: Record<string, string> = Object.fromEntries(TYPE_OPTIONS.map((o) => [o.value, o.label]));

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

// Portfolio totals over the base-currency aggregates (computed once per render).
type Totals = { cost: number; mv: number; unreal: number; unrealPct: number | null; excluded: number };

// Per-row context handed to each column's cell renderer — base currency, the editable
// portfolio id, and the no-fabrication FX fallback bound to this holding.
type RowCtx = {
  baseCurrency: string;
  portfolioId?: string;
  foreign: boolean;
  baseMoney: (base: number | null, native: number | null, fmt?: (v: number, c: string) => string) => ReactNode;
};

// Every column belongs to a BAND. Ticker + Market Value are the frozen spine (not a band); the
// optional Account column (uncombined view) pins beside them. Position / Value / Returns /
// Class hold the remaining position spine; the rest are the feed-gated analytics.
export type ColumnBand =
  | "Position"
  | "Value"
  | "Returns"
  | "Class"
  | "Attractiveness"
  | "Valuation"
  | "Fundamentals"
  | "Technicals"
  | "Consensus";

export const BAND_ORDER: ColumnBand[] = [
  "Position",
  "Value",
  "Returns",
  "Class",
  "Attractiveness",
  "Valuation",
  "Fundamentals",
  "Technicals",
  "Consensus",
];

// Unified column descriptor. Position bands carry bespoke `cell` renderers (FX fallback,
// inline editors, staleness); the analytic bands are adapted from METRIC_COLUMNS below.
type ColumnDef = {
  key: string;
  label: string;
  band: ColumnBand;
  /** Priced-only — hidden in the cost-basis-only (no-feed) view. */
  priced?: boolean;
  /** Numeric columns open descending (biggest positions / highest values first). */
  defaultDesc?: boolean;
  align?: "left" | "right";
  title?: string;
  /** Sort value — base-currency where available, native as a stable fallback. */
  sort: (h: Holding) => SortValue;
  /** Cell content (the wrapping <td> supplies padding / alignment / border). */
  cell: (h: Holding, ctx: RowCtx) => ReactNode;
  /** Optional footer (portfolio total) cell content; omitted → blank. */
  foot?: (t: Totals, baseCurrency: string) => ReactNode;
};

// ── Position spine columns, now band-grouped (metron-ops#118+). Each keeps the bespoke
// rendering it had as a hard-coded cell: FX fallback (baseMoney), stale-price ⚠, accounting
// sign coloring, and the editable ClassifyCell dropdowns. ──
const POSITION_COLUMNS: ColumnDef[] = [
  // Position — quantity + the cost-basis economics (available even in the unpriced view).
  {
    key: "quantity",
    label: "Quantity",
    band: "Position",
    defaultDesc: true,
    sort: (h) => h.quantity,
    cell: (h) => quantity(h.quantity),
  },
  {
    key: "avg_cost",
    label: "Avg cost",
    band: "Position",
    defaultDesc: true,
    sort: (h) => (h.fx_rate != null ? h.avg_cost * h.fx_rate : h.avg_cost),
    cell: (h, ctx) => ctx.baseMoney(h.fx_rate != null ? h.avg_cost * h.fx_rate : null, h.avg_cost),
  },
  {
    key: "cost_basis",
    label: "Cost basis",
    band: "Position",
    defaultDesc: true,
    title: "Total amount paid for the position (quantity × average cost), in the base currency.",
    sort: (h) => h.cost_basis_base ?? h.cost_basis,
    cell: (h, ctx) => ctx.baseMoney(h.cost_basis_base, h.cost_basis, moneyWhole),
    foot: (t, ccy) => moneyWhole(t.cost, ccy),
  },
  // Value — live price + unrealized (priced). Market value itself is rendered as part of the
  // frozen spine (MARKET_VALUE_COLUMN below), not this band.
  {
    key: "last",
    label: "Last",
    band: "Value",
    priced: true,
    defaultDesc: true,
    sort: (h) => (h.last_price != null && h.fx_rate != null ? h.last_price * h.fx_rate : h.last_price),
    cell: (h, ctx) => {
      const lastBase = h.last_price != null && h.fx_rate != null ? h.last_price * h.fx_rate : null;
      return (
        <span
          className={h.last_price_stale ? "text-amber-500" : "text-muted"}
          title={
            h.last_price_stale && h.last_price_date
              ? `Stale — last close ${h.last_price_date}; the market-data feed hasn’t updated since`
              : undefined
          }
        >
          {ctx.baseMoney(lastBase, h.last_price)}
          {h.last_price_stale ? <span className="ml-0.5" aria-hidden>⚠</span> : null}
          {h.is_estimated ? (
            <span
              className="ml-0.5 text-sky-500"
              title="Estimated — this fund hasn't struck its own NAV yet today; its same-day move is estimated from a tracking-proxy ETF and will reconcile to the true NAV after tomorrow's close."
              aria-label="estimated"
            >
              ~
            </span>
          ) : null}
        </span>
      );
    },
  },
  {
    key: "unrealized",
    label: "Unrealized $",
    band: "Value",
    priced: true,
    defaultDesc: true,
    title: "Paper gain/loss if sold now: market value − cost basis (base currency). Excludes realized gains + dividends.",
    sort: (h) => h.unrealized_gain,
    cell: (h, ctx) => (
      <span className={signClass(h.unrealized_gain ?? 0)}>
        {h.unrealized_gain != null ? accountingMoneyWhole(h.unrealized_gain, ctx.baseCurrency) : "—"}
      </span>
    ),
    foot: (t, ccy) => <span className={signClass(t.unreal)}>{accountingMoneyWhole(t.unreal, ccy)}</span>,
  },
  {
    key: "unrealized_pct",
    label: "Unrealized %",
    band: "Value",
    priced: true,
    defaultDesc: true,
    title: "Unrealized gain/loss as a % of cost basis (the position's total return so far, ex-dividends).",
    sort: (h) => h.unrealized_pct,
    cell: (h) => (
      <span className={signClass(h.unrealized_pct ?? 0)}>
        {h.unrealized_pct != null ? accountingPercent(h.unrealized_pct) : "—"}
      </span>
    ),
    foot: (t) => (
      <span className={signClass(t.unrealPct ?? 0)}>{t.unrealPct != null ? accountingPercent(t.unrealPct) : "—"}</span>
    ),
  },
  // Returns — per-security period returns (metron-ops#87): Day (overnight/intraday), YTD, LTM.
  {
    key: "day_pct",
    label: "Day",
    band: "Returns",
    priced: true,
    defaultDesc: true,
    title: "Today's price return — overnight (open vs prior close) + intraday (latest vs open). Needs the live feed.",
    sort: (h) => h.day_pct,
    cell: (h) => (
      <span
        className={h.day_pct != null ? signClass(h.day_pct) : "text-muted"}
        title={
          h.overnight_pct != null && h.intraday_pct != null
            ? `overnight ${accountingPercent(h.overnight_pct)} · intraday ${accountingPercent(h.intraday_pct)}`
            : undefined
        }
      >
        {h.day_pct != null ? accountingPercent(h.day_pct) : "—"}
      </span>
    ),
  },
  {
    key: "ytd_pct",
    label: "YTD",
    band: "Returns",
    priced: true,
    defaultDesc: true,
    title: "Price return year-to-date (since the last close of the prior year), from cached daily closes.",
    sort: (h) => h.ytd_pct,
    cell: (h) => (
      <span className={h.ytd_pct != null ? signClass(h.ytd_pct) : "text-muted"}>
        {h.ytd_pct != null ? accountingPercent(h.ytd_pct) : "—"}
      </span>
    ),
  },
  {
    key: "ltm_pct",
    label: "LTM",
    band: "Returns",
    priced: true,
    defaultDesc: true,
    title: "Price return over the last twelve months (trailing 1-year), from cached daily closes.",
    sort: (h) => h.ltm_pct,
    cell: (h) => (
      <span className={h.ltm_pct != null ? signClass(h.ltm_pct) : "text-muted"}>
        {h.ltm_pct != null ? accountingPercent(h.ltm_pct) : "—"}
      </span>
    ),
  },
  // Class — currency + the editable reference classification (sector / country / type). These
  // resolve without a market feed, so the band shows in the cost-basis-only view too.
  {
    key: "fx",
    label: "FX",
    band: "Class",
    sort: (h) => h.currency,
    cell: (h, ctx) => (
      <span className="text-muted">
        {ctx.foreign ? `${h.currency} @ ${h.fx_rate != null ? fxRate(h.fx_rate) : "—"}` : h.currency}
      </span>
    ),
  },
  {
    key: "sector",
    label: "Sector",
    band: "Class",
    sort: (h) => h.sector,
    cell: (h, ctx) => <ClassifyCellContent h={h} field="sector" portfolioId={ctx.portfolioId} />,
  },
  {
    key: "country",
    label: "Country",
    band: "Class",
    sort: (h) => h.country,
    cell: (h, ctx) => <ClassifyCellContent h={h} field="country" portfolioId={ctx.portfolioId} />,
  },
  {
    key: "type",
    label: "Type",
    band: "Class",
    sort: (h) => h.security_type,
    cell: (h, ctx) => <ClassifyCellContent h={h} field="type" portfolioId={ctx.portfolioId} />,
  },
];

type MetricColumn = {
  key: string;
  label: string;
  group: ColumnBand;
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

// Unit sub-scores ∈ [0, 1] band around the 0.5 neutral midpoint (matches the tearsheet gauge
// breakdown's convention — see ATTRACTIVENESS_COMPONENT_LABELS in the tearsheet page).
const subScoreTone = (v: number): string =>
  v >= 0.6 ? "text-positive" : v <= 0.4 ? "text-negative" : "";

const METRIC_COLUMNS: MetricColumn[] = [
  // ── Attractiveness — composite score (metron-ops#106, Phase 2) plus its full component
  // breakdown, the same inspectable sub-scores the tearsheet gauge shows (metron-ops#130). A
  // component is "—" when its input was missing and dropped from the renormalized blend. ──
  {
    key: "attractiveness",
    label: "Score",
    group: "Attractiveness",
    value: (h) => h.attractiveness,
    render: (v) => decimal(v, 1),
    tone: attractivenessTone,
    title:
      "Composite attractiveness (0–100): transparent blend of fwd-P/E vs sector median, " +
      "price-target upside, consensus rating, revision momentum, and news sentiment. " +
      "Click the ticker to open the holding's tearsheet for the weighted breakdown.",
  },
  {
    key: "attractiveness_valuation",
    label: "Val",
    group: "Attractiveness",
    value: (h) => h.attractiveness_valuation,
    render: (v) => decimal(v, 2),
    tone: subScoreTone,
    title: "Valuation sub-score (0–1): forward P/E vs the sector/country median — cheaper is more attractive.",
  },
  {
    key: "attractiveness_upside",
    label: "Ups",
    group: "Attractiveness",
    value: (h) => h.attractiveness_upside,
    render: (v) => decimal(v, 2),
    tone: subScoreTone,
    title: "Upside sub-score (0–1): mean analyst price target vs the live price.",
  },
  {
    key: "attractiveness_rating",
    label: "Rtg",
    group: "Attractiveness",
    value: (h) => h.attractiveness_rating,
    render: (v) => decimal(v, 2),
    tone: subScoreTone,
    title: "Rating sub-score (0–1): analyst consensus rating (strongBuy…strongSell) remapped to unit scale.",
  },
  {
    key: "attractiveness_revision",
    label: "Rev",
    group: "Attractiveness",
    value: (h) => h.attractiveness_revision,
    render: (v) => decimal(v, 2),
    tone: subScoreTone,
    title: "Revision sub-score (0–1): estimate-revision momentum. Paid feed — dropped until it lands (metron-ops#107).",
  },
  {
    key: "attractiveness_sentiment",
    label: "Sent",
    group: "Attractiveness",
    value: (h) => h.attractiveness_sentiment,
    render: (v) => decimal(v, 2),
    tone: subScoreTone,
    title: "Sentiment sub-score (0–1): news sentiment, trust-weighted Loughran-McDonald composite.",
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
  // ── Fundamentals (full financial-statement picture, metron-ops#140: growth/margins/returns
  // AND balance-sheet leverage/liquidity in one band — institutional platforms don't split
  // balance-sheet health out as an unrelated axis from the rest of the fundamentals) ──
  { key: "rev_growth", label: "Rev Gr", group: "Fundamentals", value: (h) => h.rev_growth, render: (v) => percent(v), signed: true },
  { key: "earnings_growth", label: "EPS Gr", group: "Fundamentals", value: (h) => h.earnings_growth, render: (v) => percent(v), signed: true },
  { key: "gross_margin", label: "Gross M", group: "Fundamentals", value: (h) => h.gross_margin, render: (v) => pct1(v) },
  { key: "op_margin", label: "Op M", group: "Fundamentals", value: (h) => h.op_margin, render: (v) => pct1(v) },
  { key: "roe", label: "ROE", group: "Fundamentals", value: (h) => h.roe, render: (v) => percent(v), signed: true },
  { key: "roa", label: "ROA", group: "Fundamentals", value: (h) => h.roa, render: (v) => percent(v), signed: true },
  { key: "beta", label: "Beta", group: "Fundamentals", value: (h) => h.beta, render: (v) => decimal(v, 2) },
  { key: "cash", label: "Cash", group: "Fundamentals", value: (h) => h.cash, render: (v, base) => marketCapShort(v, base), title: "Total cash & equivalents" },
  { key: "debt", label: "Debt", group: "Fundamentals", value: (h) => h.debt, render: (v, base) => marketCapShort(v, base), title: "Total debt" },
  { key: "net_debt", label: "Net Debt", group: "Fundamentals", value: (h) => h.net_debt, render: (v, base) => marketCapShort(v, base), title: "Total debt − total cash (negative = net cash)" },
  { key: "debt_to_equity", label: "D/E", group: "Fundamentals", value: (h) => h.debt_to_equity, render: (v) => decimal(v / 100, 2), title: "Debt / equity (ratio)" },
  { key: "net_debt_to_ebitda", label: "ND/EBITDA", group: "Fundamentals", value: (h) => h.net_debt_to_ebitda, render: (v) => decimal(v, 2), title: "Net debt / EBITDA — leverage" },
  { key: "current_ratio", label: "Cur R", group: "Fundamentals", value: (h) => h.current_ratio, render: (v) => decimal(v, 2), title: "Current ratio (liquidity)" },
  { key: "quick_ratio", label: "Quick R", group: "Fundamentals", value: (h) => h.quick_ratio, render: (v) => decimal(v, 2), title: "Quick ratio (acid-test liquidity)" },
  { key: "fcf", label: "FCF", group: "Fundamentals", value: (h) => h.fcf, render: (v, base) => marketCapShort(v, base), signed: true, title: "Free cash flow (TTM)" },
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

/** Adapt a declarative MetricColumn (value/render/tone/text) into a unified ColumnDef so the
 *  analytic bands render through the same loop as the position bands. */
function metricToColumnDef(c: MetricColumn): ColumnDef {
  return {
    key: c.key,
    label: c.label,
    band: c.group,
    priced: true,
    defaultDesc: true,
    align: "right",
    title: c.title,
    sort: c.value,
    cell: (h, ctx) => {
      const v = c.value(h);
      const tone = v == null ? "text-muted" : c.tone ? c.tone(v) : c.signed ? signClass(v) : "";
      // Categorical columns (e.g. the consensus rating) render their own label; numeric
      // columns render from the non-null value.
      const content = c.text ? c.text(h) : v == null ? null : c.render(v, ctx.baseCurrency);
      return <span className={content == null ? "text-muted" : tone}>{content == null ? "—" : content}</span>;
    },
  };
}

// Every column (position spine + analytics) in one list, keyed by band for the grouped header.
const ALL_COLUMNS: ColumnDef[] = [...POSITION_COLUMNS, ...METRIC_COLUMNS.map(metricToColumnDef)];

// Market Value — held constant in the frozen spine beside Ticker (metron-ops#140) rather than
// living inside the toggleable Value band, so every column-set preset anchors on the same
// two reference columns without needing to drag the rest of Value/Position along. Not part of
// ALL_COLUMNS (it never renders through the band loop) but still registered for sort.
const MARKET_VALUE_COLUMN: ColumnDef = {
  key: "market_value",
  label: "Market value",
  band: "Value",
  priced: true,
  defaultDesc: true,
  sort: (h) => h.market_value ?? h.market_value_local,
  cell: (h, ctx) => ctx.baseMoney(h.market_value, h.market_value_local, moneyWhole),
  foot: (t, ccy) => moneyWhole(t.mv, ccy),
};

// Lookup over EVERY sortable column, so header clicks sort uniformly.
const SORT_BY_KEY = new Map<string, (h: Holding) => SortValue>(
  [...ALL_COLUMNS, MARKET_VALUE_COLUMN].map((c) => [c.key, c.sort] as const),
);
const DESC_BY_DEFAULT = new Set<string>(
  [...ALL_COLUMNS, MARKET_VALUE_COLUMN].filter((c) => c.defaultDesc).map((c) => c.key),
);

// Sticky first (ticker) column — stays put while the bands scroll right.
const STICKY = "sticky left-0 z-10";

export function HoldingsTable({
  holdings,
  baseCurrency,
  priced,
  portfolioId,
  visibleBands = BAND_ORDER,
  accountColumn = false,
  showTotals = true,
  showMarketValue = true,
  onRemove,
}: {
  holdings: Holding[];
  baseCurrency: string;
  priced: boolean;
  /** When set, the Ticker cell exposes an inline alias editor (metron-ops#47). */
  portfolioId?: string;
  /** Which bands to render, in canonical order (column presets, metron-ops#114/#118+).
   *  Defaults to every band; the Ticker + Market Value spine is always shown. */
  visibleBands?: ColumnBand[];
  /** Render an Account column (the uncombined per-account view, metron-ops#114). The rows
   *  must carry `account_label`; it pins beside Ticker in the frozen spine. */
  accountColumn?: boolean;
  /** The Position/Value footer totals only mean something for real positions — a
   *  comparison-only row set (e.g. the watchlist) passes false to drop the row entirely. */
  showTotals?: boolean;
  /** Market Value is part of the frozen spine by default (metron-ops#140) — a row set with no
   *  real position (e.g. the watchlist compare table) passes false so it doesn't render a
   *  column of nothing but "—". */
  showMarketValue?: boolean;
  /** When set, renders a trailing "Remove" column (e.g. the watchlist compare table,
   *  metron-ops#121) — absent for the read-only Holdings view. */
  onRemove?: (ticker: string) => void;
}) {
  // Visible columns grouped by band, in canonical order, with priced-only bands dropped in
  // the cost-basis-only view (a band whose every column is priced collapses to nothing).
  const visibleSet = new Set(visibleBands);
  const colsByBand = BAND_ORDER.filter((b) => visibleSet.has(b))
    .map((b) => [b, ALL_COLUMNS.filter((c) => c.band === b && (priced || !c.priced))] as const)
    .filter(([, cols]) => cols.length > 0);
  const visibleColumns = colsByBand.flatMap(([, cols]) => cols);
  const marketValueVisible = priced && showMarketValue;
  const spineCols = 1 + (accountColumn ? 1 : 0) + (marketValueVisible ? 1 : 0);

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
  const totals = useMemo<Totals>(() => {
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

  // Header sort-control (shared by the spine + band column headers).
  // `title` is the column's plain-language DEFINITION (when it has one): the header shows a
  // small ⓘ signpost so the definition is discoverable via a real click-to-open disclosure —
  // NOT nested inside the sort <button> (nested buttons are invalid HTML and swallow every
  // click into `toggle(colKey)`, so the ⓘ was unreachable — metron-ops#115 follow-up, metron#158).
  const SortTh = ({ colKey, label, title }: { colKey: string; label: string; title?: string }) => (
    <span className="inline-flex items-center gap-1 uppercase tracking-wide">
      <button
        type="button"
        onClick={() => toggle(colKey)}
        className="inline-flex items-center gap-1 hover:text-ink"
        title={`Sort by ${label}`}
      >
        {label}
        <span className={sort?.key === colKey ? "" : "invisible"}>{sort?.desc ? "▼" : "▲"}</span>
      </button>
      {title ? (
        // Native <details> disclosure — click-to-open, keyboard/focus accessible, no
        // click-outside wiring needed. Same pattern as the Columns "Customize" control
        // (holdings-column-presets.tsx).
        <details className="relative inline-block normal-case leading-none">
          <summary
            className="cursor-help list-none text-[10px] font-normal text-muted/70 marker:hidden hover:text-ink [&::-webkit-details-marker]:hidden"
            aria-label={`What is ${label}?`}
          >
            ⓘ
          </summary>
          <div className="absolute left-0 top-full z-30 mt-1 w-56 rounded-lg border border-line bg-paper p-2 text-[11px] font-normal normal-case leading-snug text-ink shadow-lg">
            {title}
          </div>
        </details>
      ) : null}
    </span>
  );

  return (
    <DualScroll deps={`${spineCols}:${holdings.length}:${priced}:${visibleColumns.length}`}>
      <table className="w-full text-sm">
        <thead className="sticky top-0 z-20">
          {colsByBand.length > 0 ? (
            // Band-label row: an empty cell over the Ticker spine, then one cell per band.
            <tr className="border-b border-line bg-surface text-left text-[10px] uppercase tracking-wider text-muted">
              <th colSpan={spineCols} className={`${STICKY} bg-surface px-3 py-1.5`} />
              {colsByBand.map(([band, cols]) => (
                <th
                  key={band}
                  colSpan={cols.length}
                  className="border-l border-line px-3 py-1.5 text-center font-semibold text-accent"
                >
                  {band}
                </th>
              ))}
              {onRemove ? <th className="bg-surface px-3 py-1.5" /> : null}
            </tr>
          ) : null}
          <tr className="border-b border-line bg-surface text-left text-xs uppercase tracking-wide text-muted">
            <th className={`${STICKY} bg-surface px-3 py-2 font-medium`}>
              <SortTh colKey="ticker" label="Ticker" />
            </th>
            {accountColumn ? (
              <th className="px-3 py-2 text-left font-medium">
                <SortTh colKey="account" label="Account" />
              </th>
            ) : null}
            {marketValueVisible ? (
              <th className="px-3 py-2 text-right font-medium">
                <SortTh colKey={MARKET_VALUE_COLUMN.key} label={MARKET_VALUE_COLUMN.label} />
              </th>
            ) : null}
            {colsByBand.map(([, cols]) =>
              cols.map((col, j) => (
                <th
                  key={col.key}
                  className={`px-3 py-2 font-medium ${col.align === "left" ? "text-left" : "text-right"} ${j === 0 ? "border-l border-line" : ""}`}
                >
                  <SortTh colKey={col.key} label={col.label} title={col.title} />
                </th>
              )),
            )}
            {onRemove ? <th className="px-3 py-2 text-right font-medium">Remove</th> : null}
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
            const ctx: RowCtx = { baseCurrency, portfolioId, foreign, baseMoney };
            return (
              <tr key={h.ticker} className="border-b border-line last:border-0">
                <td className={`${STICKY} bg-paper px-3 py-2 font-medium`}>
                  <TickerCell h={h} portfolioId={portfolioId} />
                </td>
                {accountColumn ? (
                  <td className="px-3 py-2 text-left text-muted">{h.account_label ?? "—"}</td>
                ) : null}
                {marketValueVisible ? (
                  <td className="px-3 py-2 text-right tabular-nums">{MARKET_VALUE_COLUMN.cell(h, ctx)}</td>
                ) : null}
                {colsByBand.map(([, cols]) =>
                  cols.map((col, j) => (
                    <td
                      key={col.key}
                      className={`px-3 py-2 tabular-nums ${col.align === "left" ? "text-left" : "text-right"} ${j === 0 ? "border-l border-line" : ""}`}
                      title={col.title}
                    >
                      {col.cell(h, ctx)}
                    </td>
                  )),
                )}
                {onRemove ? (
                  <td className="px-3 py-2 text-right">
                    <button
                      type="button"
                      onClick={() => onRemove(h.ticker)}
                      aria-label={`Remove ${h.ticker}`}
                      className="rounded px-2 py-0.5 text-xs text-muted hover:bg-rose-500/10 hover:text-rose-300"
                    >
                      Remove
                    </button>
                  </td>
                ) : null}
              </tr>
            );
          })}
        </tbody>
        {holdings.length > 0 && showTotals ? (
          <tfoot>
            <tr className="border-t border-line bg-surface font-medium">
              <td
                className={`${STICKY} bg-surface px-3 py-2`}
                title={totals.excluded > 0 ? `${totals.excluded} holding(s) excluded — no ${baseCurrency} FX rate cached` : undefined}
              >
                Total{totals.excluded > 0 ? "*" : ""}
              </td>
              {accountColumn ? <td className="px-3 py-2" /> : null}
              {marketValueVisible ? (
                <td className="px-3 py-2 text-right tabular-nums">
                  {MARKET_VALUE_COLUMN.foot ? MARKET_VALUE_COLUMN.foot(totals, baseCurrency) : null}
                </td>
              ) : null}
              {colsByBand.map(([, cols]) =>
                cols.map((col, j) => (
                  <td
                    key={col.key}
                    className={`px-3 py-2 text-right tabular-nums ${j === 0 ? "border-l border-line" : ""}`}
                  >
                    {col.foot ? col.foot(totals, baseCurrency) : null}
                  </td>
                )),
              )}
              {onRemove ? <td className="px-3 py-2" /> : null}
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

/** The Sector / Country / Type cell content (no <td> — the band loop wraps it). With a
 *  portfolioId it's editable: an UNCLASSIFIED holding shows a "Set …" dropdown directly (the
 *  gap the user wants to fill); a classified one shows the value with a small ✎ to correct it.
 *  Choosing the blank option clears the override (reverts to the spine-resolved value).
 *  Read-only contexts just render the value. The override is tenant-scoped (it never mutates
 *  the shared reference row). */
function ClassifyCellContent({
  h,
  field,
  portfolioId,
}: {
  h: Holding;
  field: "sector" | "country" | "type";
  portfolioId?: string;
}) {
  const router = useRouter();
  // Type rides on security_type (a key like "treasury" shown via a friendly label); sector /
  // country are free strings where the value IS the label.
  const value = field === "sector" ? h.sector : field === "country" ? h.country : h.security_type;
  const display = field === "type" ? (value ? TYPE_LABEL[value] ?? value : null) : value;
  const [editing, setEditing] = useState(false);
  const [pending, start] = useTransition();
  const [error, setError] = useState<string | null>(null);

  // Read-only context (no portfolioId) — just the value.
  if (!portfolioId) {
    return <span className="text-muted">{display ?? "—"}</span>;
  }

  // Options as {value,label}: a curated list for sector/country (value == label, current
  // value always offered so an override never drops a non-canonical value), the fixed
  // key/label set for type.
  const options: { value: string; label: string }[] =
    field === "type"
      ? TYPE_OPTIONS
      : (() => {
          const base = field === "sector" ? SECTOR_OPTIONS : COUNTRY_OPTIONS;
          const list = value && !base.includes(value) ? [value, ...base] : base;
          return list.map((o) => ({ value: o, label: o }));
        })();

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

  // Show the dropdown directly when unclassified (the gap to fill — sector/country), or when
  // the user clicked ✎ to correct an existing value. Type is always classified → edit-only.
  const showSelect = value == null || editing;
  if (showSelect) {
    return (
      <>
        <select
          value={value ?? ""}
          disabled={pending}
          onChange={(e) => choose(e.target.value)}
          aria-label={`Set ${field} for ${h.ticker}`}
          className="max-w-[10rem] rounded border border-line bg-surface px-1.5 py-0.5 text-xs"
        >
          <option value="">{value == null ? `Set ${field}…` : `— clear ${field} —`}</option>
          {options.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
        {error ? <div className="mt-0.5 text-[10px] text-negative">{error}</div> : null}
      </>
    );
  }

  return (
    <span className="inline-flex items-center gap-1 text-muted">
      {display}
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
