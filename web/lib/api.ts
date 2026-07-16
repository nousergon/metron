// Typed server-side client for the Metron FastAPI backend.
//
// Types mirror the pydantic response models in api/routers/portfolios.py. Calls run
// in Server Components / Server Actions (never the browser); the caller resolves an
// API credential from the auth session (`requireApiAuth` in lib/session.ts) and passes
// it in as `apiAuth`: a short-lived bearer JWT minted by the shared nousergon-auth
// identity service (metron-ops#179), or — for the signup-free read-only demo — the
// fixed demo tenant id, the ONE value the backend still accepts via X-Tenant-Id.

import { DEMO_TENANT_ID } from "@/lib/demo";

const API_URL = process.env.METRON_API_URL ?? "http://localhost:8000";

/** Auth headers for one backend request. The demo credential IS the fixed demo tenant
 * id (a real session's JWT can never equal it), so this is a deterministic branch on a
 * named constant — not a fallback: any other non-JWT value would go out as a Bearer
 * token and be 401'd by the backend, never silently trusted as a tenant id. */
function authHeaders(apiAuth: string): Record<string, string> {
  return apiAuth === DEMO_TENANT_ID ? { "X-Tenant-Id": apiAuth } : { Authorization: `Bearer ${apiAuth}` };
}

/** Stable per-identity cache key for `unstable_cache` keyParts/tags (lib/entitlements,
 * lib/account-meta). The credential itself is unusable as a key: identity JWTs are
 * short-lived and re-minted per render, so keying on the raw token would silently
 * reduce every cached read to a miss and break tag-based revalidation across mints.
 * The JWT `sub` claim (the shared nousergon-auth user id) is stable for the life of
 * the account and maps 1:1 to a workspace via users.identity_user_id — so it isolates
 * tenants exactly. Decode-without-verify is safe here: the token came from the trusted
 * auth service over TLS and is only used as a cache key; the BACKEND verifies the
 * signature on every actual data fetch. */
export function cacheIdentity(apiAuth: string): string {
  if (apiAuth === DEMO_TENANT_ID) return apiAuth;
  const payload = apiAuth.split(".")[1];
  if (!payload) throw new Error("cacheIdentity: credential is neither the demo id nor a JWT");
  const { sub } = JSON.parse(Buffer.from(payload, "base64url").toString("utf8")) as { sub?: string };
  if (!sub) throw new Error("cacheIdentity: identity JWT has no sub claim");
  return sub;
}

export type Portfolio = { id: string; name: string; base_currency: string };

export type Holding = {
  ticker: string;
  quantity: number;
  avg_cost: number; // native per-share cost
  cost_basis: number; // native total cost basis
  currency: string;
  fx_rate: number | null; // base per 1 unit of `currency` (1.0 for USD)
  // Null until prices are refreshed; populated from the cached EOD close.
  // `_local` fields are native; market_value / cost_basis_base are base-currency.
  last_price: number | null;
  last_price_date: string | null;
  // True when the close-fed price is ≥1 full trading session stale (upstream EOD feed
  // stalled). Drives the Holdings "prices as of" warning. False on broker-snapshot /
  // live-intraday paths.
  last_price_stale: boolean;
  // True when last_price is a same-day ESTIMATE synthesized from a tracking-proxy ETF's
  // return (metron-ops#112) — a late-striking mutual fund (e.g. FNILX/FZILX/FTIHX) that
  // hasn't struck its own NAV yet today. Not a problem flag like last_price_stale — an
  // expected, clearly-labeled estimate, reconciled to the true struck NAV tomorrow.
  is_estimated: boolean;
  // Broker-reported "as of" date for this position (IBKR Flex statement / SnapTrade
  // last_holdings_sync) — how current the SHARE COUNT is, distinct from last_price_date
  // (how current the PRICE is). null for ledger-only (CSV/OFX) holdings, which have no
  // broker snapshot to go stale.
  broker_as_of: string | null;
  // True when a snapshot-sourced holding's broker_as_of is stale (metron-ops#150) — the
  // daily broker re-sync hasn't run recently, so a real trade at the broker may not yet
  // be reflected here even though last_price looks fresh. Drives the Holdings "positions
  // as of" staleness warning. Always false for ledger-only holdings.
  positions_stale: boolean;
  market_value_local: number | null;
  cost_basis_base: number | null;
  market_value: number | null;
  unrealized_gain: number | null;
  unrealized_pct: number | null;
  // Coarse asset class for grouping (cash / bond / equity / etf / fund / option / other).
  security_type: string;
  // Account attribution — set only on the uncombined (by-account) view, where one row is
  // one (account, ticker). null on the default consolidated view (metron-ops#114).
  account_id: string | null;
  account_label: string | null;
  // User-set display label/alias (so a numeric-CUSIP bond is legible). null when unset.
  user_label: string | null;
  // Per-security period returns (metron-ops#87). Day legs (overnight/intraday/day) need the
  // intraday feed → null off a feed-entitled build; YTD/LTM from cached daily closes.
  overnight_pct: number | null;
  intraday_pct: number | null;
  day_pct: number | null;
  // Position-level day value change in base currency ($) — live-gated like day_pct.
  day_change: number | null;
  ytd_pct: number | null;
  ltm_pct: number | null;
  // Reference classification (cached from the data spine). GICS sector + country of
  // domicile; country drives the US-vs-international split. null = unclassified gap.
  sector: string | null;
  country: string | null;
  // Valuation / fundamentals / technicals metrics for the Holdings table columns —
  // feed-gated (yfinance data spine). null off a feed-entitled build or on a coverage gap.
  market_cap: number | null;
  pe: number | null;
  fwd_pe: number | null;
  eps: number | null; // $ trailing EPS — raw input behind pe
  fwd_eps: number | null; // $ forward EPS — raw input behind fwd_pe
  pb: number | null;
  book_value_per_share: number | null; // $ — raw input behind pb
  ps: number | null;
  revenue_per_share: number | null; // $ — raw input behind ps
  ev_ebitda: number | null;
  ebitda: number | null; // $ raw EBITDA — input behind ev_ebitda
  enterprise_value: number | null; // $ — the other input behind ev_ebitda
  peg: number | null;
  div_yield: number | null; // fraction
  rev_growth: number | null; // fraction
  earnings_growth: number | null; // fraction
  gross_margin: number | null; // fraction
  op_margin: number | null; // fraction
  roe: number | null; // fraction
  roa: number | null; // fraction
  beta: number | null;
  // Balance sheet (absolute $ + leverage/liquidity).
  cash: number | null; // total cash ($)
  debt: number | null; // total debt ($)
  net_debt: number | null; // debt − cash ($)
  debt_to_equity: number | null; // yfinance raw (a percentage, e.g. 47.2)
  net_debt_to_ebitda: number | null; // (debt − cash) / EBITDA
  current_ratio: number | null;
  quick_ratio: number | null;
  fcf: number | null; // free cash flow ($)
  rsi_14: number | null;
  macd_hist: number | null;
  pct_to_ma_50: number | null; // fraction
  pct_to_ma_200: number | null; // fraction
  pct_in_52w_range: number | null; // 0-1
  mom_20d: number | null; // fraction
  // Consensus research + news sentiment (metron-ops#105) — feed-gated, free-source data
  // spine. null off a feed-entitled build or on a coverage gap, never fabricated.
  consensus_rating: string | null; // strongBuy/buy/hold/sell/strongSell
  consensus_score: number | null; // signed [-1, +1] (strongBuy=+1 … strongSell=-1)
  price_target_mean: number | null; // mean analyst target (native price units)
  price_target_median: number | null;
  price_target_upside: number | null; // mean target / last_price − 1 (fraction)
  num_analysts: number | null;
  news_sentiment: number | null; // trust-weighted LM composite ∈ [-1, +1]
  news_articles: number | null;
  // SOTA 6-pillar attractiveness from NE factor profiles. null off-feed or outside scanner universe.
  attractiveness: number | null;
  attractiveness_coverage: number | null;
  attractiveness_quality: number | null;
  attractiveness_value: number | null;
  attractiveness_momentum: number | null;
  attractiveness_growth: number | null;
  attractiveness_stewardship: number | null;
  attractiveness_defensiveness: number | null;
};

// Sector- / country-level median multiples (SP1500-broad peer benchmark) for the Holdings
// "by sector → country" bands. Fields null when the producer had no usable sample.
export type GroupMedians = {
  n: number;
  trailing_pe: number | null;
  forward_pe: number | null;
  price_to_book: number | null;
  price_to_sales: number | null;
  ev_ebitda: number | null;
  dividend_yield: number | null; // fraction
};

export type ValuationMedians = {
  as_of: string | null;
  by_sector: Record<string, GroupMedians>;
  by_country: Record<string, GroupMedians>;
};

export type IncomeYear = {
  year: number;
  realized_st: number;
  realized_lt: number;
  dividends: number;
  interest: number;
  distributions: number; // taxable withdrawals from tax-deferred accounts (Trad IRA / 401k, incl. RMDs)
  net_capital_gains: number;
  taxable_income: number;
};

export type Account = {
  account_id: string;
  broker: string;
  external_id: string;
  name: string;
  currency: string;
  nickname: string | null;
  institution: string | null;
  account_type: string | null;
  tax_treatment: string | null;
  taxable: boolean;
  // Per-account valuation (base currency); null until prices are cached.
  cost_basis_base: number | null;
  market_value: number | null;
  unrealized_gain: number | null;
  n_unconverted: number;
  // Per-account period returns (metron-ops#87). Day legs need the intraday feed; YTD/LTM
  // from the per-account reconstructed NAV series. null when unavailable.
  overnight_pct: number | null;
  intraday_pct: number | null;
  day_pct: number | null;
  ytd_pct: number | null;
  ltm_pct: number | null;
};

/** Cacheable selector metadata only (metron-ops#91 Part 2) — deliberately excludes
 *  every valuation/return field on `Account` so this shape is safe to short-TTL cache
 *  client-side (see lib/account-meta.ts). Use `Account`/`getAccounts` (no-store) for
 *  anything that needs live market_value/day_pct. */
export type AccountMeta = {
  account_id: string;
  broker: string;
  external_id: string;
  name: string;
  currency: string;
  nickname: string | null;
  institution: string | null;
  account_type: string | null;
  tax_treatment: string | null;
  taxable: boolean;
};

export type AccountDetail = {
  account: Account;
  holdings: Holding[];
  realized: RealizedLot[];
  transactions: Transaction[];
};

export type Transaction = {
  trade_date: string; // ISO date
  txn_type: string;
  ticker: string;
  quantity: number;
  price: number;
  amount: number; // native
  fees: number;
  currency: string;
  fx_rate: number | null; // trade-date base-per-unit (1.0 for USD)
  amount_base: number | null;
};

export type RealizedLot = {
  ticker: string;
  open_date: string; // ISO date
  close_date: string; // ISO date
  quantity: number;
  proceeds: number; // native
  cost_basis: number; // native
  gain: number; // native
  long_term: boolean;
  currency: string;
  fx_rate: number | null; // close-date base-per-unit (1.0 for USD)
  gain_base: number | null; // gain in base currency at the close-date rate
  proceeds_base: number | null;
  cost_basis_base: number | null;
};

export type Summary = {
  base_currency: string;
  n_accounts: number;
  n_holdings: number;
  total_cost_basis: number;
  realized_st: number;
  realized_lt: number;
  realized_total: number;
  realized_st_ytd: number; // taxable, current calendar year
  realized_lt_ytd: number; // taxable, current calendar year
  realized_ytd_taxadv: number; // tax-advantaged YTD total (never taxed)
  dividends: number;
  interest: number;
  distributions: number;
  taxable_income: number;
  market_value: number | null;
  unrealized_gain: number | null;
  n_unconverted: number;
};

export type PriceRefreshResult = {
  symbols_requested: number;
  prices_updated: number;
  snapshot_recorded: boolean;
};

export type PerfPoint = {
  snap_date: string;
  nav: number;
  external_flow: number;
  spy_close: number | null;
};

export type RollingRiskPoint = {
  snap_date: string;
  volatility: number | null;
  sharpe: number | null;
  sortino: number | null;
  max_drawdown: number | null;
  psr: number | null;
  cvar: number | null;
};

export type Performance = {
  n_snapshots: number;
  first_date: string | null;
  last_date: string | null;
  days: number;
  latest_nav: number | null;
  latest_cost_basis: number | null;
  net_contributions: number;
  cumulative_return: number | null;
  twr: number | null;
  annualized_twr: number | null;
  volatility: number | null;
  sharpe: number | null;
  sortino: number | null;
  max_drawdown: number | null;
  psr: number | null;
  cvar: number | null;
  spy_return: number | null;
  alpha: number | null;
  rolling: RollingRiskPoint[];
  points: PerfPoint[];
  estimated: boolean;
  estimated_note: string | null;
};

/** One benchmark's comparison over a period tile (metron-ops#83). */
export type BenchmarkReturn = {
  symbol: string;
  label: string;
  ret: number | null;   // benchmark % return over the window
  alpha: number | null; // portfolio %TWR − benchmark %
};

/** One Overview hero tile: aggregate holdings performance over a window. */
export type PeriodTile = {
  period: string;       // "today" | "ytd" | "ltm"
  label: string;        // "Today" | "YTD" | "LTM"
  start_date: string | null;
  end_date: string | null;
  gain: number | null;  // $ investment gain over the window (net of external flows)
  twr: number | null;   // % time-weighted return over the window
  benchmarks: BenchmarkReturn[];
  note?: string | null; // honest empty-state reason (e.g. TODAY "as of <prior date>")
};

/** Overview performance-vs-market tiles. Benchmark comparison is feed-gated (Pro):
 *  `benchmarks_available=false` in the no-feed beta → portfolio-only tiles. */
export type PeriodTiles = {
  tiles: PeriodTile[];
  benchmarks_available: boolean;
  last_date: string | null;
};

/** A point on a normalized growth series (g=1.0 at the series' first point). */
export type SeriesPoint = { when: string; g: number };

/** One account's performance line for the Holdings chart (metron-ops#78, #87).
 *  `coverage`: "reconstructed" = deep history rebuilt from lots/transactions;
 *  "forward" = accrues from when tracking began (SnapTrade & other snapshot accounts). */
export type AccountSeries = {
  account_id: string;
  name: string;
  points: SeriesPoint[];
  coverage: "reconstructed" | "forward";
};

/** One benchmark overlay (SPY/QQQ/IWM) for the Holdings chart. */
export type BenchmarkSeries = { symbol: string; label: string; points: SeriesPoint[] };

/** Per-account performance lines + benchmark overlays. Each series is normalized to 1.0
 *  at its first point so the client re-ranges + re-bases to 100 without a refetch.
 *  Benchmark overlays are feed-gated (Pro): empty + `benchmarks_available=false` in beta. */
export type HoldingsPerfSeries = {
  accounts: AccountSeries[];
  benchmarks: BenchmarkSeries[];
  benchmarks_available: boolean;
};

export class MetronApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

async function get<T>(apiAuth: string, path: string): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, {
    headers: authHeaders(apiAuth),
    cache: "no-store",
  });
  if (!res.ok) {
    throw new MetronApiError(res.status, `GET ${path} → ${res.status}`);
  }
  return res.json() as Promise<T>;
}

/** Build the repeatable `?account_id=` selection query (empty when no/zero ids → whole portfolio). */
export function acctParams(accountIds?: string[]): string {
  if (!accountIds || accountIds.length === 0) return "";
  return "?" + accountIds.map((a) => `account_id=${encodeURIComponent(a)}`).join("&");
}

/** The authenticated caller's resolved workspace — the deploy-verification chokepoint
 * for the shared-identity cutover (metron-ops#179). */
export const getIdentity = (apiAuth: string) => get<{ tenant_id: string }>(apiAuth, "/me");

export const getPortfolios = (apiAuth: string) => get<Portfolio[]>(apiAuth, "/portfolios");
export const getPortfolio = (apiAuth: string, id: string) => get<Portfolio>(apiAuth, `/portfolios/${id}`);

// Watchlist — tracked tickers (held or not). No live price (un-held tickers have no price
// source until the Pro feed). On a feed-entitled build carries the SAME Holdings metrics
// (valuation/fundamentals/balance-sheet/technicals/consensus/attractiveness), keyed purely
// by ticker, for side-by-side comparison — never quantity/cost/market value/P&L, since a
// watchlist entry has no position (metron-ops#42, metron-ops#121).
export type WatchlistEntry = {
  symbol: string;
  name: string | null;
  sector: string | null;
  country: string | null;
  next_earnings_date: string | null;
  held: boolean;
  note: string | null;
  market_cap: number | null;
  pe: number | null;
  fwd_pe: number | null;
  eps: number | null;
  fwd_eps: number | null;
  pb: number | null;
  book_value_per_share: number | null;
  ps: number | null;
  revenue_per_share: number | null;
  ev_ebitda: number | null;
  ebitda: number | null;
  enterprise_value: number | null;
  peg: number | null;
  div_yield: number | null;
  rev_growth: number | null;
  earnings_growth: number | null;
  gross_margin: number | null;
  op_margin: number | null;
  roe: number | null;
  roa: number | null;
  beta: number | null;
  cash: number | null;
  debt: number | null;
  net_debt: number | null;
  debt_to_equity: number | null;
  net_debt_to_ebitda: number | null;
  current_ratio: number | null;
  quick_ratio: number | null;
  fcf: number | null;
  rsi_14: number | null;
  macd_hist: number | null;
  pct_to_ma_50: number | null;
  pct_to_ma_200: number | null;
  pct_in_52w_range: number | null;
  mom_20d: number | null;
  consensus_rating: string | null;
  consensus_score: number | null;
  price_target_mean: number | null;
  price_target_median: number | null;
  price_target_upside: number | null;
  num_analysts: number | null;
  news_sentiment: number | null;
  news_articles: number | null;
  attractiveness: number | null;
  attractiveness_coverage: number | null;
  attractiveness_quality: number | null;
  attractiveness_value: number | null;
  attractiveness_momentum: number | null;
  attractiveness_growth: number | null;
  attractiveness_stewardship: number | null;
  attractiveness_defensiveness: number | null;
};

export const getWatchlist = (apiAuth: string, id: string) =>
  get<WatchlistEntry[]>(apiAuth, `/portfolios/${id}/watchlist`);

export async function addWatchlist(
  apiAuth: string,
  id: string,
  symbol: string,
  note?: string | null,
): Promise<WatchlistEntry> {
  const res = await fetch(`${API_URL}/portfolios/${id}/watchlist`, {
    method: "POST",
    headers: { ...authHeaders(apiAuth), "Content-Type": "application/json" },
    body: JSON.stringify({ symbol, note: note ?? null }),
    cache: "no-store",
  });
  if (!res.ok) {
    let detail = `${res.status}`;
    try {
      detail = ((await res.json()) as { detail?: string }).detail ?? detail;
    } catch {
      // keep the status
    }
    throw new MetronApiError(res.status, detail);
  }
  return res.json() as Promise<WatchlistEntry>;
}

export async function removeWatchlist(apiAuth: string, id: string, symbol: string): Promise<void> {
  const res = await fetch(`${API_URL}/portfolios/${id}/watchlist/${encodeURIComponent(symbol)}`, {
    method: "DELETE",
    headers: authHeaders(apiAuth),
    cache: "no-store",
  });
  if (!res.ok) throw new MetronApiError(res.status, `DELETE watchlist → ${res.status}`);
}

// Crypto — standalone wallet-address tracking (BTC+ETH), decoupled from the EOD-close
// holdings/NAV. Balances are synced by the nousergon-data producer; Metron never calls a
// chain. A position with synced=false is awaiting its first sync. (metron-ops#111)
export type CryptoPosition = {
  id: string;
  chain: string;
  address: string;
  label: string | null;
  symbol: string | null;
  balance: number | null;
  price_usd: number | null;
  value_usd: number | null;
  synced: boolean;
};

export type CryptoSummary = {
  available: boolean;
  as_of_utc: string | null;
  stale: boolean;
  total_usd: number | null;
  n_pending: number;
  positions: CryptoPosition[];
  reason: string | null;
};

export const getCrypto = (apiAuth: string, id: string) =>
  get<CryptoSummary>(apiAuth, `/portfolios/${id}/crypto`);

export async function addCryptoAddress(
  apiAuth: string,
  id: string,
  chain: string,
  address: string,
  label?: string | null,
): Promise<CryptoPosition> {
  const res = await fetch(`${API_URL}/portfolios/${id}/crypto/addresses`, {
    method: "POST",
    headers: { ...authHeaders(apiAuth), "Content-Type": "application/json" },
    body: JSON.stringify({ chain, address, label: label ?? null }),
    cache: "no-store",
  });
  if (!res.ok) {
    let detail = `${res.status}`;
    try {
      detail = ((await res.json()) as { detail?: string }).detail ?? detail;
    } catch {
      // keep the status
    }
    throw new MetronApiError(res.status, detail);
  }
  return res.json() as Promise<CryptoPosition>;
}

export async function deleteCryptoAddress(apiAuth: string, id: string, addressId: string): Promise<void> {
  const res = await fetch(`${API_URL}/portfolios/${id}/crypto/addresses/${encodeURIComponent(addressId)}`, {
    method: "DELETE",
    headers: authHeaders(apiAuth),
    cache: "no-store",
  });
  if (!res.ok) throw new MetronApiError(res.status, `DELETE crypto address → ${res.status}`);
}

/** Set (or clear, with an empty label) a user alias for a symbol (metron-ops#47). */
export async function setSecurityLabel(
  apiAuth: string,
  id: string,
  symbol: string,
  label: string | null,
): Promise<{ symbol: string; label: string | null }> {
  const res = await fetch(`${API_URL}/portfolios/${id}/securities/${encodeURIComponent(symbol)}/label`, {
    method: "PUT",
    headers: { ...authHeaders(apiAuth), "Content-Type": "application/json" },
    body: JSON.stringify({ label }),
    cache: "no-store",
  });
  if (!res.ok) {
    let detail = `${res.status}`;
    try {
      detail = ((await res.json()) as { detail?: string }).detail ?? detail;
    } catch {
      // keep the status
    }
    throw new MetronApiError(res.status, detail);
  }
  return res.json() as Promise<{ symbol: string; label: string | null }>;
}

/** Set a tenant's GICS-sector / country-of-domicile override for a symbol so an
 * Unclassified holding can be placed in the Allocation breakdown. Only the keys present in
 * `patch` are changed — omit a key to leave it, pass `null` to clear it (clearing both
 * removes the override). Tenant-scoped; never touches the shared securities reference. */
export async function setSecurityClassification(
  apiAuth: string,
  id: string,
  symbol: string,
  patch: { sector?: string | null; country?: string | null; instrument_type?: string | null },
): Promise<{ symbol: string; sector: string | null; country: string | null; instrument_type: string | null }> {
  const res = await fetch(`${API_URL}/portfolios/${id}/securities/${encodeURIComponent(symbol)}/classification`, {
    method: "PUT",
    headers: { ...authHeaders(apiAuth), "Content-Type": "application/json" },
    body: JSON.stringify(patch),
    cache: "no-store",
  });
  if (!res.ok) {
    let detail = `${res.status}`;
    try {
      detail = ((await res.json()) as { detail?: string }).detail ?? detail;
    } catch {
      // keep the status
    }
    throw new MetronApiError(res.status, detail);
  }
  return res.json() as Promise<{ symbol: string; sector: string | null; country: string | null; instrument_type: string | null }>;
}

/** Update a portfolio's name and/or base currency (PATCH). Empty/no-op rejected (422). */
export async function updatePortfolio(
  apiAuth: string,
  id: string,
  patch: { name?: string; base_currency?: string },
): Promise<Portfolio> {
  const res = await fetch(`${API_URL}/portfolios/${id}`, {
    method: "PATCH",
    headers: { ...authHeaders(apiAuth), "Content-Type": "application/json" },
    body: JSON.stringify(patch),
    cache: "no-store",
  });
  if (!res.ok) {
    let detail = `${res.status}`;
    try {
      detail = ((await res.json()) as { detail?: string }).detail ?? detail;
    } catch {
      // keep the status
    }
    throw new MetronApiError(res.status, detail);
  }
  return res.json() as Promise<Portfolio>;
}

/** Rename a portfolio (PATCH). Empty names are rejected by the backend (422). */
export const renamePortfolio = (apiAuth: string, id: string, name: string) =>
  updatePortfolio(apiAuth, id, { name });
// The reads below take an optional `accountIds` selection (from the account panel's
// checkboxes); omitted/empty = whole portfolio. `getAccounts` is always unscoped — it
// IS the selector, so it lists every account with its own valuation.
// `valuation` selects the regime (metron-ops#153): omitted/"settled" = official EOD close
// (the conservative default — Overview and every non-asking consumer); "live" = the
// Holdings live mode's intraday overlay.
export const getSummary = (apiAuth: string, id: string, accountIds?: string[], valuation?: "live" | "settled") => {
  const base = acctParams(accountIds);
  const q = valuation === "live" ? (base ? `${base}&valuation=live` : "?valuation=live") : base;
  return get<Summary>(apiAuth, `/portfolios/${id}/summary${q}`);
};
// `byAccount` requests the UNCOMBINED view — one row per (account, ticker), each tagged
// with account_id/account_label (metron-ops#114). Default consolidates per ticker.
// `valuation` (metron-ops#153): omitted/"settled" = official EOD close (session day legs
// null); "live" = the intraday overlay + session day legs, feed/toggle permitting.
export const getHoldings = (apiAuth: string, id: string, accountIds?: string[], byAccount?: boolean, valuation?: "live" | "settled") => {
  const parts: string[] = [];
  const base = acctParams(accountIds);
  if (base) parts.push(base.slice(1));
  if (byAccount) parts.push("by_account=1");
  if (valuation === "live") parts.push("valuation=live");
  const q = parts.length ? `?${parts.join("&")}` : "";
  return get<Holding[]>(apiAuth, `/portfolios/${id}/holdings${q}`);
};
// SP1500-broad sector & country median multiples for the Holdings "by sector → country"
// bands, restricted to the sectors/countries this portfolio holds. Empty off-feed.
export const getValuationMedians = (apiAuth: string, id: string, accountIds?: string[]) =>
  get<ValuationMedians>(apiAuth, `/portfolios/${id}/valuation-medians${acctParams(accountIds)}`);
// account selection + the taxable-only flag (the Tax/Activity views default taxable —
// tax-advantaged accounts have no taxable events; metron-ops#48).
function activityQuery(accountIds?: string[], taxableOnly?: boolean): string {
  const parts: string[] = (accountIds ?? []).map((a) => `account_id=${encodeURIComponent(a)}`);
  if (taxableOnly) parts.push("taxable_only=true");
  return parts.length ? "?" + parts.join("&") : "";
}

export const getIncome = (apiAuth: string, id: string, accountIds?: string[], taxableOnly?: boolean) =>
  get<IncomeYear[]>(apiAuth, `/portfolios/${id}/income${activityQuery(accountIds, taxableOnly)}`);
export const getAccounts = (apiAuth: string, id: string) => get<Account[]>(apiAuth, `/portfolios/${id}/accounts`);
// Cacheable selector metadata only, no live valuation (metron-ops#91 Part 2) — read this
// through lib/account-meta.ts's short-TTL cache, not directly, wherever caching matters.
export const getAccountsMeta = (apiAuth: string, id: string) =>
  get<AccountMeta[]>(apiAuth, `/portfolios/${id}/accounts/meta`);
export const getTransactions = (apiAuth: string, id: string, accountIds?: string[], taxableOnly?: boolean) =>
  get<Transaction[]>(apiAuth, `/portfolios/${id}/transactions${activityQuery(accountIds, taxableOnly)}`);
export const getRealized = (apiAuth: string, id: string, accountIds?: string[], taxableOnly?: boolean) =>
  get<RealizedLot[]>(apiAuth, `/portfolios/${id}/realized${activityQuery(accountIds, taxableOnly)}`);
// `valuation` (metron-ops#149 item 1): omitted/"settled" = official EOD close; "live" =
// the intraday overlay scoped to just this account, mirroring `getHoldings`'s contract so
// the account-detail page's live markers agree with the Holdings page's.
export const getAccountDetail = (apiAuth: string, id: string, accountId: string, valuation?: "live" | "settled") =>
  get<AccountDetail>(
    apiAuth,
    `/portfolios/${id}/accounts/${accountId}${valuation === "live" ? "?valuation=live" : ""}`,
  );
export const getPerformance = (apiAuth: string, id: string, accountIds?: string[]) =>
  get<Performance>(apiAuth, `/portfolios/${id}/performance${acctParams(accountIds)}`);
export const getPerformanceTiles = (apiAuth: string, id: string, accountIds?: string[]) =>
  get<PeriodTiles>(apiAuth, `/portfolios/${id}/performance/tiles${acctParams(accountIds)}`);
export const getHoldingsPerformanceSeries = (apiAuth: string, id: string, accountIds?: string[]) =>
  get<HoldingsPerfSeries>(apiAuth, `/portfolios/${id}/holdings/performance-series${acctParams(accountIds)}`);

export type Risk = {
  computable: boolean;
  benchmark: string;
  reason: string | null;
  as_of: string | null;
  n_obs: number;
  n_modeled: number;
  excluded: string[];
  total_vol: number | null;
  factor_vol: number | null;
  idio_vol: number | null;
  idio_pct: number | null;
  tracking_error: number | null;
  factor_exposures: Record<string, number>;
  factor_pct_contrib: Record<string, number>;
};

export const getRisk = (apiAuth: string, id: string, accountIds?: string[]) =>
  get<Risk>(apiAuth, `/portfolios/${id}/risk${acctParams(accountIds)}`);

export type TaxLot = {
  ticker: string;
  open_date: string;
  quantity: number;
  currency: string;
  cost_basis: number; // native
  term: string;
  cost_basis_base: number | null;
  market_value: number | null; // base
  unrealized_gain: number | null; // base
  harvestable_loss: number | null;
};

export type Tax = {
  as_of: string;
  base_currency: string;
  n_lots: number;
  n_priced: number;
  unrealized_st: number | null;
  unrealized_lt: number | null;
  unrealized_total: number | null;
  // Authoritative unrealized for the taxable scope (position-level; reconciles to the
  // Accounts table). >= unrealized_total; the gap is in positions with incomplete history.
  unrealized_position_total: number | null;
  harvestable_loss: number | null;
  n_accounts_excluded: number;
  // Positions counted in the total but not lot-classifiable (broker history starts mid-position).
  n_incomplete: number;
  incomplete_tickers: string[];
  lots: TaxLot[];
};

export const getTax = (apiAuth: string, id: string, accountIds?: string[]) =>
  get<Tax>(apiAuth, `/portfolios/${id}/tax${acctParams(accountIds)}`);

export type Tearsheet = {
  ticker: string;
  base_currency: string;
  as_of: string;
  position: {
    ticker: string;
    currency: string;
    quantity: number;
    avg_cost: number;
    cost_basis: number | null;
    market_value: number | null;
    unrealized_gain: number | null;
    unrealized_pct: number | null;
    weight_pct: number | null;
    accounts: string[];
  };
  performance: {
    return_vs_cost: number | null;
    period_returns: Record<string, number>;
    volatility: number | null;
    sharpe: number | null;
    sortino: number | null;
    max_drawdown: number | null;
    beta_vs_spy: number | null;
    vs_spy: number | null;
    vs_spy_1y: number | null;
    n_bars: number;
    history_from: string | null;
  };
  technical: {
    rsi_14: number | null;
    pct_from_52wk_high: number | null;
    forward_div_yield: number | null;
  };
  fundamentals_available: boolean;
  fundamentals_reason: string;
  fundamentals: TickerFundamentals | null;
  fundamentals_as_of: string | null;
  comps: Comp[];
  // Consensus research + news sentiment panel (metron-ops#105) — feed-gated, free-source
  // spine. consensus_available is false off-feed or on a coverage gap.
  consensus_available: boolean;
  consensus_as_of: string | null;
  consensus: TearsheetConsensus;
  // Composite attractiveness gauge (metron-ops#106, Phase 2) — feed-gated; available is false
  // off-feed or on a total coverage gap.
  attractiveness: TearsheetAttractiveness;
};

export type TearsheetAttractivenessComponent = {
  key: string; // quality / value / momentum / growth / stewardship / defensiveness
  weight: number;
  score: number; // 0–100 sector-neutral pillar percentile
  contribution: number | null;
};

export type TearsheetAttractiveness = {
  available: boolean;
  score: number | null;
  coverage: number | null;
  components: TearsheetAttractivenessComponent[];
};

export type TearsheetConsensus = {
  consensus_rating: string | null;
  consensus_score: number | null;
  price_target_mean: number | null;
  price_target_median: number | null;
  price_target_upside: number | null; // fraction
  num_analysts: number | null;
  news_sentiment: number | null; // [-1, +1]
  news_articles: number | null;
  news_as_of: string | null;
  // Paid forward-estimate scaffolding (metron-ops#107): columns exist now, resolve to
  // `N/A · paid feed` until the paid consensus-estimates feed lands — no later schema change.
  estimates_available: boolean;
  estimates_reason: string;
  forward_eps: number | null;
  forward_revenue: number | null;
  forward_pe_consensus: number | null;
  peg_consensus: number | null;
  estimate_revision_trend: number | null;
};

export type TickerFundamentals = {
  yf_symbol: string;
  sector: string | null;
  industry: string | null;
  market_cap: number | null;
  beta: number | null;
  trailing_pe: number | null;
  forward_pe: number | null;
  peg: number | null;
  ev_ebitda: number | null;
  earnings_growth: number | null;
  revenue_growth: number | null;
  debt_to_equity: number | null;
  current_ratio: number | null;
  quick_ratio: number | null;
  roe: number | null;
  roa: number | null;
  gross_margins: number | null;
  operating_margins: number | null;
  dividend_yield: number | null;
};

export type Comp = {
  ticker: string;
  sector: string | null;
  trailing_pe: number | null;
  forward_pe: number | null;
  ev_ebitda: number | null;
  debt_to_equity: number | null;
  dividend_yield: number | null;
  is_self: boolean;
};

export const getTearsheet = (apiAuth: string, id: string, ticker: string) =>
  get<Tearsheet>(apiAuth, `/portfolios/${id}/tearsheet/${encodeURIComponent(ticker)}`);

export type SectorEffect = {
  sector: string;
  port_weight: number;
  bench_weight: number;
  port_return: number | null;
  bench_return: number | null;
  allocation: number;
  selection: number;
  interaction: number;
  total: number;
};

export type Attribution = {
  computable: boolean;
  benchmark: string;
  reason: string | null;
  as_of: string | null;
  start_date: string | null;
  lookback_days: number;
  coverage: number;
  n_sectors: number;
  portfolio_return: number | null;
  benchmark_return: number | null;
  active_return: number | null;
  allocation: number | null;
  selection: number | null;
  interaction: number | null;
  sectors: SectorEffect[];
};

export const getAttribution = (apiAuth: string, id: string, accountIds?: string[]) =>
  get<Attribution>(apiAuth, `/portfolios/${id}/attribution${acctParams(accountIds)}`);

// Concentration & diversification diagnostics (metron-ops-I167) — deterministic
// portfolio-structure FACTS on the settled context. Types mirror DiagnosticsOut.
export type DiagnosticsSectorRow = {
  sector: string;
  weight: number; // share of included (priced, non-cash) market value
  market_value: number;
  benchmark_weight: number | null; // null: benchmark unavailable or non-benchmark bucket
  delta: number | null; // weight − benchmark_weight (overweight > 0)
};

export type DiagnosticsGeoRow = {
  bucket: string; // "US" | "International" | "Unclassified"
  weight: number;
  market_value: number;
};

export type DiagnosticsConcentration = {
  n_positions: number;
  hhi: number;
  effective_n: number;
  top5_share: number;
  top10_share: number;
  max_position_ticker: string;
  max_position_weight: number;
};

export type TargetDriftRow = {
  kind: "allocation" | "max_position" | "avoid_sector";
  label: string;
  target: number | null;
  actual: number | null; // null = the stated target isn't measurable from holdings metadata
  breach: boolean | null; // null for pure drift rows (no boolean rule) / unmeasurable rows
  detail: string | null;
};

export type Diagnostics = {
  computable: boolean;
  reason: string | null;
  required_tier: string | null;
  as_of: string | null; // the settled close date the valuation used — the card's badge
  base_currency: string;
  total_market_value: number;
  benchmark: string;
  benchmark_available: boolean;
  benchmark_reason: string | null; // "tier"/"feed"/"benchmark" (locked) or "unavailable"
  benchmark_required_tier: string | null;
  concentration: DiagnosticsConcentration | null;
  sectors: DiagnosticsSectorRow[];
  geography: DiagnosticsGeoRow[];
  // null = the user has authored no targets (the drift section doesn't render).
  target_drift: TargetDriftRow[] | null;
};

export const getDiagnostics = (apiAuth: string, id: string, accountIds?: string[]) =>
  get<Diagnostics>(apiAuth, `/portfolios/${id}/diagnostics${acctParams(accountIds)}`);

export type MacroPoint = { obs_date: string; value: number };

export type MacroIndicator = {
  key: string;
  label: string;
  units: string;
  latest_value: number;
  latest_date: string;
  prior_value: number | null;
  change: number | null;
  next_release: string | null; // next scheduled release date (metron-ops#49)
  history: MacroPoint[];
};

export type Macro = {
  available: boolean;
  reason: string | null;
  as_of: string | null;
  indicators: MacroIndicator[];
};

// Macro is global market data; the tenant header is sent for client consistency but
// the endpoint ignores it. `full` requests the deep per-indicator history for the Macro
// detail-page charts (~1y); the default lean window powers the Overview strip.
export const getMacro = (apiAuth: string, opts?: { full?: boolean }) =>
  get<Macro>(apiAuth, `/macro${opts?.full ? "?full=true" : ""}`);

// ── Research intel (neutral market intel from crucible-research; paid Intelligence tier) ──
// EPIC config#1499 Phase 1 / metron-ops#117. Global, read-only intel: regime + narrative,
// sector ratings/modifiers, market breadth, per-ticker attractiveness + generic thesis.
export type ResearchIntelSectorRating = { rating: string | null; rationale: string | null };
export type ResearchIntelBreakdown = {
  quant_score: number | null;
  qual_score: number | null;
  factor_subscore: number | null;
  weighted_base: number | null;
  macro_shift: number | null;
};
export type ResearchIntelThesis = { bull_case: string | null; sector: string | null };
export type ResearchIntelAttractiveness = {
  ticker: string;
  score: number | null;
  sector: string | null;
  breakdown: ResearchIntelBreakdown | null;
  thesis: ResearchIntelThesis | null;
};
export type ResearchIntelSnapshot = {
  schema_version: number | null;
  date: string | null;
  generated_at: string | null;
  market_regime: string | null;
  regime_narrative: string | null;
  sector_ratings: Record<string, ResearchIntelSectorRating>;
  sector_modifiers: Record<string, number>;
  market_breadth: {
    pct_above_50d_ma: number | null;
    pct_above_200d_ma: number | null;
    advance_decline_ratio: number | null;
  };
  attractiveness: Record<string, ResearchIntelAttractiveness>;
};
export type ResearchIntel = {
  available: boolean;
  reason: string | null;
  required_tier: string | null;
  // null until the first weekly artifact is cached (available-but-stale); null intel
  // when not entitled either — the surface never blanks and never leaks intel.
  stale: boolean | null;
  intel: ResearchIntelSnapshot | null;
};

// `tickers` scopes the attractiveness map to the caller's holdings (empty ⇒ full universe).
export const getResearchIntel = (apiAuth: string, opts?: { tickers?: string[] }) => {
  const q = opts?.tickers && opts.tickers.length > 0 ? `?tickers=${encodeURIComponent(opts.tickers.join(","))}` : "";
  return get<ResearchIntel>(apiAuth, `/research-intel${q}`);
};

export type CalendarEvent = { event_date: string; kind: string; ticker: string; label: string };

export type Calendar = {
  as_of: string;
  horizon_days: number;
  n_events: number;
  events: CalendarEvent[];
  // When the earnings dates behind `events` were last (re)sourced (metron-ops#149 item 2)
  // — null until a held ticker has ever been through a refresh.
  earnings_sourced_at: string | null;
};

export const getCalendar = (apiAuth: string, id: string) =>
  get<Calendar>(apiAuth, `/portfolios/${id}/calendar`);

/** Refresh held-ticker earnings dates (yfinance), then return the calendar (heavier POST). */
export async function refreshCalendar(apiAuth: string, id: string): Promise<Calendar> {
  const res = await fetch(`${API_URL}/portfolios/${id}/calendar/refresh`, {
    method: "POST",
    headers: authHeaders(apiAuth),
    cache: "no-store",
  });
  if (!res.ok) {
    throw new MetronApiError(res.status, `refresh calendar → ${res.status}`);
  }
  return res.json() as Promise<Calendar>;
}

/** Resolve sectors + backfill history, then run Brinson attribution (heavier POST).
 * `accountIds` scopes the computation to the selected accounts. */
export async function computeAttribution(apiAuth: string, id: string, accountIds?: string[]): Promise<Attribution> {
  const res = await fetch(`${API_URL}/portfolios/${id}/attribution/compute${acctParams(accountIds)}`, {
    method: "POST",
    headers: authHeaders(apiAuth),
    cache: "no-store",
  });
  if (!res.ok) {
    throw new MetronApiError(res.status, `compute attribution → ${res.status}`);
  }
  return res.json() as Promise<Attribution>;
}

/** Backfill history + compute factor risk (the heavier POST path). `accountIds` scopes it. */
export async function computeRisk(apiAuth: string, id: string, accountIds?: string[]): Promise<Risk> {
  const res = await fetch(`${API_URL}/portfolios/${id}/risk/compute${acctParams(accountIds)}`, {
    method: "POST",
    headers: authHeaders(apiAuth),
    cache: "no-store",
  });
  if (!res.ok) {
    throw new MetronApiError(res.status, `compute risk → ${res.status}`);
  }
  return res.json() as Promise<Risk>;
}

/** Seed NAV history from past prices (reconstruction). Returns the populated summary. */
export async function reconstructPerformance(apiAuth: string, id: string): Promise<Performance> {
  const res = await fetch(`${API_URL}/portfolios/${id}/performance/reconstruct`, {
    method: "POST",
    headers: authHeaders(apiAuth),
    cache: "no-store",
  });
  if (!res.ok) {
    throw new MetronApiError(res.status, `reconstruct → ${res.status}`);
  }
  return res.json() as Promise<Performance>;
}

/** Create a portfolio in the user's workspace (auto-provisions the tenant on the backend). */
export async function createPortfolio(apiAuth: string, name: string): Promise<Portfolio> {
  const res = await fetch(`${API_URL}/portfolios`, {
    method: "POST",
    headers: { ...authHeaders(apiAuth), "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
    cache: "no-store",
  });
  if (!res.ok) {
    throw new MetronApiError(res.status, `create portfolio → ${res.status}`);
  }
  return res.json() as Promise<Portfolio>;
}

// --- imports (write) -------------------------------------------------------

export type SkipRecord = { ref: string; reason: string };

export type ImportResult = {
  source: string;
  rows_parsed: number;
  rows_skipped: number;
  accounts_created: number;
  securities_created: number;
  transactions_inserted: number;
  transactions_skipped: number;
  positions_imported: number;
  errors: SkipRecord[];
};

async function readResult(res: Response, label: string): Promise<ImportResult> {
  if (!res.ok) {
    // Surface the backend's detail (e.g. 422 "missing date column", 502 Flex error).
    let detail = `${res.status}`;
    try {
      const body = (await res.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      /* non-JSON error body — keep the status */
    }
    throw new MetronApiError(res.status, `${label}: ${detail}`);
  }
  return res.json() as Promise<ImportResult>;
}

/** Upload a CSV or OFX file to an import endpoint. ``kind`` selects the route. */
export async function importFile(
  apiAuth: string,
  id: string,
  kind: "csv" | "ofx",
  file: File,
): Promise<ImportResult> {
  const form = new FormData();
  form.append("file", file, file.name);
  const res = await fetch(`${API_URL}/portfolios/${id}/import/${kind}`, {
    method: "POST",
    headers: authHeaders(apiAuth),
    body: form,
    cache: "no-store",
  });
  return readResult(res, `${kind.toUpperCase()} import`);
}

/** Add one manually-entered stock/ETF position — the no-brokerage, no-file path
 * (metron-ops#187) alongside CSV/OFX/Flex/SnapTrade. `costBasis` is the TOTAL cost
 * basis for the position (not per-share); `tradeDate` (YYYY-MM-DD) defaults to today
 * server-side when omitted. Returns the same `ImportResult` shape as every other
 * import route — one summary contract regardless of source. */
export async function addManualPosition(
  apiAuth: string,
  id: string,
  position: { ticker: string; quantity: number; costBasis: number; tradeDate?: string | null },
): Promise<ImportResult> {
  const res = await fetch(`${API_URL}/portfolios/${id}/positions/manual`, {
    method: "POST",
    headers: { ...authHeaders(apiAuth), "Content-Type": "application/json" },
    body: JSON.stringify({
      ticker: position.ticker,
      quantity: position.quantity,
      cost_basis: position.costBasis,
      trade_date: position.tradeDate || null,
    }),
    cache: "no-store",
  });
  return readResult(res, "Manual position");
}

export async function syncFlex(apiAuth: string, id: string, token: string, queryId: string): Promise<ImportResult> {
  const res = await fetch(`${API_URL}/portfolios/${id}/import/flex`, {
    method: "POST",
    headers: { ...authHeaders(apiAuth), "Content-Type": "application/json" },
    body: JSON.stringify({ token, query_id: queryId }),
    cache: "no-store",
  });
  return readResult(res, "IBKR Flex sync");
}

/** One-click IBKR sync from the deployment's STORED Flex credentials (metron-ops#82) —
 * no token paste. 404 when none are configured (the UI shows the BYO-token form instead). */
export async function syncFlexStored(apiAuth: string, id: string): Promise<ImportResult> {
  const res = await fetch(`${API_URL}/portfolios/${id}/sync/flex`, {
    method: "POST",
    headers: authHeaders(apiAuth),
    cache: "no-store",
  });
  return readResult(res, "IBKR Flex sync");
}

/** Deployment connector capabilities — drives which one-click sync buttons the UI shows. */
export type Meta = {
  engine: string;
  connectors: { flex_stored: boolean; snaptrade_personal: boolean };
};
export const getMeta = (apiAuth: string) => get<Meta>(apiAuth, "/meta");

/** Sync the operator's linked SnapTrade brokerages into a portfolio (every linked
 * connection, minus any the portfolio has excluded). Personal/single-operator only —
 * 404 when the deployment hasn't enabled it. */
export async function syncSnapTrade(apiAuth: string, id: string): Promise<ImportResult> {
  const res = await fetch(`${API_URL}/portfolios/${id}/import/snaptrade`, {
    method: "POST",
    headers: authHeaders(apiAuth),
    cache: "no-store",
  });
  return readResult(res, "SnapTrade sync");
}

// --- SnapTrade connections (personal/single-operator) -----------------------

export type SnapTradeConnection = {
  id: string;
  brokerage: string;
  disabled: boolean; // needs a reconnect through the portal
  n_accounts: number;
  excluded: boolean; // this portfolio's sync skips it (linked = synced by default)
};

export type SnapTradeConnections = {
  connections: SnapTradeConnection[];
  // SnapTrade-sourced accounts already imported into this portfolio. 0 while connections
  // exist = "linked but never synced" (metron-ops#21).
  n_synced_accounts: number;
};

export const getSnapTradeConnections = (apiAuth: string, id: string) =>
  get<SnapTradeConnections>(apiAuth, `/portfolios/${id}/snaptrade/connections`);

async function readDetailError(res: Response, label: string): Promise<MetronApiError> {
  let detail = `${res.status}`;
  try {
    const body = (await res.json()) as { detail?: string };
    if (body.detail) detail = body.detail;
  } catch {
    /* non-JSON error body — keep the status */
  }
  return new MetronApiError(res.status, `${label}: ${detail}`);
}

/** A short-lived SnapTrade connection-portal URL — opening it links a NEW brokerage
 * (E*TRADE, Schwab, …) or, with `reconnectId`, repairs an existing connection in
 * place (no new plan slot). The portal hosts the brokerage login; no credentials
 * touch Metron. */
export async function createSnapTradeConnectUrl(
  apiAuth: string,
  id: string,
  reconnectId?: string,
): Promise<string> {
  const res = await fetch(`${API_URL}/portfolios/${id}/snaptrade/connect`, {
    method: "POST",
    headers: { ...authHeaders(apiAuth), "Content-Type": "application/json" },
    body: JSON.stringify(reconnectId ? { reconnect: reconnectId } : {}),
    cache: "no-store",
  });
  if (!res.ok) throw await readDetailError(res, "SnapTrade connect");
  const body = (await res.json()) as { redirect_uri: string };
  return body.redirect_uri;
}

/** Permanently delete a brokerage connection at SnapTrade (frees a plan slot).
 * Irreversible — re-linking later creates a brand-new connection. Metron's stored
 * data is untouched; the connection's accounts just stop refreshing. */
export async function removeSnapTradeConnection(
  apiAuth: string,
  id: string,
  authorizationId: string,
): Promise<void> {
  const res = await fetch(
    `${API_URL}/portfolios/${id}/snaptrade/connections/${encodeURIComponent(authorizationId)}`,
    {
      method: "DELETE",
      headers: authHeaders(apiAuth),
      cache: "no-store",
    },
  );
  if (!res.ok) throw await readDetailError(res, "SnapTrade remove");
}

/** Toggle a connection's sync opt-out (exclude=true skips it on future syncs;
 * already-imported data stays). Keyed by stable connection id — no name matching. */
export async function setSnapTradeConnectionExcluded(
  apiAuth: string,
  id: string,
  authorizationId: string,
  excluded: boolean,
): Promise<void> {
  const verb = excluded ? "exclude" : "include";
  const res = await fetch(
    `${API_URL}/portfolios/${id}/snaptrade/connections/${encodeURIComponent(authorizationId)}/${verb}`,
    {
      method: "POST",
      headers: authHeaders(apiAuth),
      cache: "no-store",
    },
  );
  if (!res.ok) throw await readDetailError(res, `SnapTrade ${verb}`);
}

/** Refresh the EOD price cache for a portfolio's held tickers (market value follows). */
export async function refreshPrices(apiAuth: string, id: string): Promise<PriceRefreshResult> {
  const res = await fetch(`${API_URL}/portfolios/${id}/prices/refresh`, {
    method: "POST",
    headers: authHeaders(apiAuth),
    cache: "no-store",
  });
  if (!res.ok) {
    throw new MetronApiError(res.status, `price refresh → ${res.status}`);
  }
  return res.json() as Promise<PriceRefreshResult>;
}

// --- settings (account tags + investor preferences) ------------------------

export type AccountTagPatch = {
  nickname?: string | null;
  institution?: string | null;
  account_type?: string | null;
  // 3-way type: "taxable" | "tax_deferred" | "tax_exempt", or null for Auto-derive.
  tax_treatment?: string | null;
  taxable_override?: boolean | null;
};

/** Edit an account's tags (institution / type / taxable override). Returns the
 * account with its recomputed `taxable` status. Omitted fields are left as-is. */
export async function updateAccountTags(
  apiAuth: string,
  id: string,
  accountId: string,
  patch: AccountTagPatch,
): Promise<Account> {
  const res = await fetch(`${API_URL}/portfolios/${id}/accounts/${accountId}`, {
    method: "PATCH",
    headers: { ...authHeaders(apiAuth), "Content-Type": "application/json" },
    body: JSON.stringify(patch),
    cache: "no-store",
  });
  if (!res.ok) {
    throw new MetronApiError(res.status, `update account → ${res.status}`);
  }
  return res.json() as Promise<Account>;
}

/** Delete a connected account + all its data; its broker:external_id key joins the
 * exclusion list so future syncs skip it (restore from Settings). */
export async function deleteAccount(
  apiAuth: string,
  id: string,
  accountId: string,
): Promise<{ account_id: string; excluded_key: string }> {
  const res = await fetch(`${API_URL}/portfolios/${id}/accounts/${accountId}`, {
    method: "DELETE",
    headers: authHeaders(apiAuth),
    cache: "no-store",
  });
  if (!res.ok) {
    throw new MetronApiError(res.status, `delete account → ${res.status}`);
  }
  return res.json();
}

export type ExcludedAccount = { key: string; broker: string; external_id: string };

/** Deleted broker accounts (imports skip these keys) — restorable from Settings. */
export const getExcludedAccounts = (apiAuth: string, id: string) =>
  get<{ excluded: ExcludedAccount[] }>(apiAuth, `/portfolios/${id}/accounts/excluded`);

/** Drop a key from the exclusion list — the next sync re-imports that account. */
export async function restoreExcludedAccount(
  apiAuth: string,
  id: string,
  key: string,
): Promise<{ excluded: ExcludedAccount[] }> {
  const res = await fetch(`${API_URL}/portfolios/${id}/accounts/excluded/restore`, {
    method: "POST",
    headers: { ...authHeaders(apiAuth), "Content-Type": "application/json" },
    body: JSON.stringify({ key }),
    cache: "no-store",
  });
  if (!res.ok) {
    throw new MetronApiError(res.status, `restore account → ${res.status}`);
  }
  return res.json();
}

/** The saved accounts-panel selection (empty = whole portfolio). */
export const getAccountSelection = async (apiAuth: string, id: string): Promise<string[]> =>
  (await get<{ account_ids: string[] }>(apiAuth, `/portfolios/${id}/accounts/selection`)).account_ids;

/** Save the accounts-panel selection (empty list clears it = whole portfolio). */
export async function putAccountSelection(
  apiAuth: string,
  id: string,
  accountIds: string[],
): Promise<void> {
  const res = await fetch(`${API_URL}/portfolios/${id}/accounts/selection`, {
    method: "PUT",
    headers: { ...authHeaders(apiAuth), "Content-Type": "application/json" },
    body: JSON.stringify({ account_ids: accountIds }),
    cache: "no-store",
  });
  if (!res.ok) {
    throw new MetronApiError(res.status, `save selection → ${res.status}`);
  }
}

// Saved Holdings-table view (metron-ops#114): grouping mode + combine-across-accounts +
// hidden types + valuation regime. All null = the page default.
export type HoldingsViewPrefs = {
  grouping: string | null;
  // DEPRECATED 2026-07-08 — the column preset is session-only (landing always opens on
  // Overview); the client sends null and ignores the returned value.
  visible_bands: string[] | null;
  combine_by_account: boolean | null;
  hidden_types: string[] | null;
  valuation: string | null; // "live" | "settled" (metron-ops#153); null = page default
};

export const getHoldingsView = (apiAuth: string, id: string) =>
  get<HoldingsViewPrefs>(apiAuth, `/portfolios/${id}/holdings-view`);

/** Persist the Holdings-table view (fire-and-forget from the toolbar controls). */
export async function putHoldingsView(apiAuth: string, id: string, prefs: HoldingsViewPrefs): Promise<void> {
  const res = await fetch(`${API_URL}/portfolios/${id}/holdings-view`, {
    method: "PUT",
    headers: { ...authHeaders(apiAuth), "Content-Type": "application/json" },
    body: JSON.stringify(prefs),
    cache: "no-store",
  });
  if (!res.ok) {
    throw new MetronApiError(res.status, `save holdings view → ${res.status}`);
  }
}

export type Preferences = {
  risk_tolerance: string | null;
  objective: string | null;
  notes: string | null;
  intraday_enabled?: boolean | null;
};

export const getPreferences = (apiAuth: string, id: string) =>
  get<Preferences>(apiAuth, `/portfolios/${id}/preferences`);

/** Create or update the portfolio's investor preferences (PUT, idempotent). */
export async function putPreferences(
  apiAuth: string,
  id: string,
  prefs: Preferences,
): Promise<Preferences> {
  const res = await fetch(`${API_URL}/portfolios/${id}/preferences`, {
    method: "PUT",
    headers: { ...authHeaders(apiAuth), "Content-Type": "application/json" },
    body: JSON.stringify(prefs),
    cache: "no-store",
  });
  if (!res.ok) {
    throw new MetronApiError(res.status, `save preferences → ${res.status}`);
  }
  return res.json() as Promise<Preferences>;
}

// ---------------------------------------------------------------------------
// Extension point — premium plugins (metron-ops). On a stock public deploy no
// plugins are installed, so `getPlugins` returns [] and none of the Intelligence
// surface below is ever reached. Types mirror the metron-ops Intelligence router.
// ---------------------------------------------------------------------------

/** Nav metadata for one active out-of-tree plugin (GET /meta/plugins). */
export type PluginNav = { id: string; label: string; href: string; tier: string };

/** Active premium plugins for this deploy (empty on the public tier). */
export const getPlugins = (apiAuth: string) => get<PluginNav[]>(apiAuth, "/meta/plugins");

// ── Product-tier entitlements (GET /meta/entitlements) ───────────────────────
// Drives the owner-only tier simulator + (later) real subscription gating. A
// feature is `available` only when its tier includes it AND its data sources are
// provisioned; `reason` ("tier" / "feed" / "benchmark" / "etf_vendor") and
// `required_tier` let the UI render an honest locked state.

/** One feature's availability under the active tier + feed state. */
export type Entitlement = {
  key: string;
  label: string;
  requires: string[];
  available: boolean;
  in_tier: boolean;
  computable: boolean;
  reason: string | null;
  required_tier: string | null;
};

/** Resolved entitlements for the active (or previewed) tier. */
export type Entitlements = {
  tier: string;
  feed_enabled: boolean;
  provisioned_sources: string[];
  features: Entitlement[];
  tiers: { key: string; label: string }[];
  simulator: boolean;
};

/** Resolve entitlements; `preview` overrides are honored server-side ONLY when the
 * tier simulator is enabled (owner-only — ignored on the public product). */
export const getEntitlements = (
  apiAuth: string,
  preview?: { tier?: string; feed?: boolean },
) => {
  const params = new URLSearchParams();
  if (preview?.tier) params.set("preview_tier", preview.tier);
  if (preview?.feed !== undefined) params.set("preview_feed", String(preview.feed));
  const qs = params.toString();
  return get<Entitlements>(apiAuth, `/meta/entitlements${qs ? `?${qs}` : ""}`);
};

// ── Market indices (intraday) — the Overview "markets" strip ────────────────
export type IndexQuote = {
  symbol: string;
  label: string;
  last: number | null;
  prev_close: number | null;
  open: number | null;
  change: number | null;
  change_pct: number | null; // "Today" return
  session_date: string | null;
  suspect: boolean;
  // Period returns from cached daily closes (metron-ops#87); null when unavailable.
  ytd_pct: number | null;
  ltm_pct: number | null;
};

/** SPY/QQQ/IWM intraday proxies for the S&P 500 / Nasdaq 100 / Russell 2000.
 *  Feed-gated (Pro): `available=false` + `required_tier` when locked in the no-feed
 *  beta; `available=false` with no `required_tier` when entitled but no data yet. */
export type Indices = {
  available: boolean;
  reason: string | null;
  required_tier: string | null;
  as_of_utc: string | null;
  stale: boolean;
  indices: IndexQuote[];
};

/** Latest intraday index levels. `preview` is forwarded as X-Preview-* headers and is
 *  honored server-side ONLY when the tier simulator is on (owner-only) — mirrors the
 *  feed-dependent compute endpoints. */
export async function getIndices(
  apiAuth: string,
  preview?: { tier?: string; feed?: boolean },
): Promise<Indices> {
  const headers: Record<string, string> = { ...authHeaders(apiAuth) };
  if (preview?.tier) headers["X-Preview-Tier"] = preview.tier;
  if (preview?.feed !== undefined) headers["X-Preview-Feed"] = String(preview.feed);
  const res = await fetch(`${API_URL}/indices/intraday`, { headers, cache: "no-store" });
  if (!res.ok) throw new MetronApiError(res.status, `GET /indices/intraday → ${res.status}`);
  return res.json() as Promise<Indices>;
}

// Live-valuation status (metron-ops#79): whether the headline NAV + position values are
// currently recomputed from intraday balances, and how fresh the snapshot is. Drives the
// "intraday · ~15-min delayed · as of HH:MM" label + the client poll that re-renders the
// page so NAV stays fresh while Metron is open.
export type IntradayStatus = {
  applied: boolean;
  as_of_utc: string | null;
  stale: boolean;
  n_priced: number;
  // Live coverage (metron-ops#146): positions in scope vs positions that actually got a
  // fresh quote — un-priced ones silently keep their EOD close, so a partial overlay is
  // disclosed ("n/N live") rather than reading as fully live. n_estimated counts the
  // late-striking-fund proxy estimates (metron-ops#112) inside n_priced.
  n_total: number;
  n_estimated: number;
  reason: string | null;
  // NAV-weighted coverage (metron-ops#152): base-$ market value of the covered
  // (delayed/estimated) positions vs the whole valued portfolio, in live-NAV terms —
  // the honest "covers $X of $Y NAV" disclosure. Null when the overlay isn't applied.
  covered_nav: number | null;
  total_nav: number | null;
  // Per-ticker pricing source (metron-ops#152): "delayed" | "estimated" | "last_close" |
  // "unpriced" — derived, never a manual flag. Empty when the overlay isn't applied.
  sources: Record<string, string>;
  // Market/session state (metron-ops-I156): "live" (in session) / "recap" (today's
  // session closed — the snapshot is its closing state) / "closed" (pre-market /
  // weekend / holiday). Drives the valuation toggle's label + availability.
  session_state: "live" | "recap" | "closed";
};

export async function getIntradayStatus(apiAuth: string, id: string, accountIds?: string[]): Promise<IntradayStatus> {
  return get<IntradayStatus>(apiAuth, `/portfolios/${id}/intraday${acctParams(accountIds)}`);
}

// Today view (metron-ops#23): per-holding prior-close/open/latest + overnight·intraday·day
// P&L decomposition (% and base-$ legs) + portfolio totals, from the intraday spine quotes.
export type TodayRow = {
  ticker: string;
  label: string;
  quantity: number;
  currency: string;
  prev_close: number | null;
  open: number | null;
  last: number | null;
  overnight_pct: number | null;
  intraday_pct: number | null;
  day_pct: number | null;
  overnight_gain: number | null;
  intraday_gain: number | null;
  day_gain: number | null;
};

// A held position excluded from the covered live-session basis, with the reason
// (metron-ops#152) — drives the explicit not-in-live-session disclosure.
export type TodayExcludedRow = {
  ticker: string;
  label: string;
  reason: string; // "suspect" | "no_quote" | "no_fx"
};

export type Today = {
  available: boolean;
  base_currency: string;
  reason: string | null;
  as_of_utc: string | null;
  stale: boolean;
  n_priced: number;
  n_excluded: number;
  overnight_gain: number | null;
  intraday_gain: number | null;
  day_gain: number | null;
  overnight_pct: number | null;
  intraday_pct: number | null;
  day_pct: number | null;
  // Covered-basis denominator (metron-ops#152): prior-close base-$ MV of the decomposable
  // rows only — excluded holdings are in neither the leg $ nor this.
  covered_prev_mv: number | null;
  rows: TodayRow[];
  excluded_rows: TodayExcludedRow[];
};

export const getToday = (apiAuth: string, id: string, accountIds?: string[]) =>
  get<Today>(apiAuth, `/portfolios/${id}/today${acctParams(accountIds)}`);

// Overnight/intraday/day decomposition HISTORY (metron-ops#87) — accrues forward; the
// cumulative split shows how much of the portfolio's drift arrives overnight vs intraday.
export type IntradayLegDay = { when: string; overnight_pct: number | null; intraday_pct: number | null; day_pct: number | null };
export type IntradayLegHistory = {
  days: IntradayLegDay[];
  cum_overnight_pct: number | null;
  cum_intraday_pct: number | null;
  cum_day_pct: number | null;
  n_days: number;
};
export const getIntradayLegs = (apiAuth: string, id: string) =>
  get<IntradayLegHistory>(apiAuth, `/portfolios/${id}/intraday-legs`);

export type AdvisorSectorWeight = { sector: string; weight_pct: number; flag: string };
export type AdvisorConcentration = { ticker: string; weight_pct: number; limit_pct: number };
export type AdvisorGeo = {
  us_pct: number;
  intl_pct: number;
  unknown_pct: number;
  us_target_pct: number | null;
  intl_target_pct: number | null;
  us_gap_pp: number | null;
  intl_gap_pp: number | null;
};
export type AdvisorIncome = {
  annual_income: number;
  portfolio_yield_pct: number;
  income_target: number | null;
  income_gap: number | null;
} | null;

export type AdvisorAnalysis = {
  nav: number;
  n_holdings: number;
  geo: AdvisorGeo;
  sectors: AdvisorSectorWeight[];
  concentration: AdvisorConcentration[];
  income: AdvisorIncome;
};

export type AdvisorCommentary = {
  narrative: string;
  considerations: string[];
  cost_usd: number | null;
  generated_at: string | null;
  model: string;
  posture: string;
  fresh: boolean;
} | null;

export type AdvisorView = {
  analysis: AdvisorAnalysis;
  has_profile: boolean;
  signature: string;
  commentary: AdvisorCommentary;
};

export type AdvisorProfile = {
  strategy: string;
  risk_tolerance: string;
  time_horizon: string;
  target_allocation: Record<string, number>;
  overweight_sectors: string[];
  avoid_sectors: string[];
  income_target: number | null;
  max_single_position: number | null;
  rebalance_frequency: string;
};

/** The Intelligence view (gap analysis + cached commentary) for a portfolio. */
export const getAdvisor = (apiAuth: string, id: string) =>
  get<AdvisorView>(apiAuth, `/ext/advisor/${id}`);

export const getAdvisorProfile = (apiAuth: string, id: string) =>
  get<AdvisorProfile>(apiAuth, `/ext/advisor/${id}/profile`);

/** Run the Claude narrative for the current state (the one paid path). */
export async function generateAdvisor(apiAuth: string, id: string): Promise<AdvisorView> {
  const res = await fetch(`${API_URL}/ext/advisor/${id}/generate`, {
    method: "POST",
    headers: authHeaders(apiAuth),
    cache: "no-store",
  });
  if (!res.ok) {
    let detail = `${res.status}`;
    try {
      detail = ((await res.json()) as { detail?: string }).detail ?? detail;
    } catch {
      // non-JSON body — keep the status
    }
    throw new MetronApiError(res.status, detail);
  }
  return res.json() as Promise<AdvisorView>;
}

/** Save the tenant's investor profile (the targets the Intelligence feature compares against). */
export async function putAdvisorProfile(
  apiAuth: string,
  id: string,
  profile: AdvisorProfile,
): Promise<AdvisorProfile> {
  const res = await fetch(`${API_URL}/ext/advisor/${id}/profile`, {
    method: "PUT",
    headers: { ...authHeaders(apiAuth), "Content-Type": "application/json" },
    body: JSON.stringify(profile),
    cache: "no-store",
  });
  if (!res.ok) {
    throw new MetronApiError(res.status, `save profile → ${res.status}`);
  }
  return res.json() as Promise<AdvisorProfile>;
}

// --- Alpha Engine overlay (metron-ops, personal-tier) ---

export type AlphaHolding = {
  ticker: string;
  quantity: number;
  market_value: number | null;
  tracked: boolean;
  signal: string | null;
  rating: string | null;
  score: number | null;
  conviction: string | null;
  thesis_summary: string | null;
  predicted_direction: string | null;
  prediction_confidence: number | null;
  predicted_alpha: number | null;
  momentum_veto: boolean | null;
};

export type AlphaCoverage = { n_holdings: number; n_tracked: number; n_exit: number; n_veto: number };

export type AlphaBuyCandidate = { ticker: string; score?: number; [k: string]: unknown };

export type AlphaEngineView = {
  available: boolean;
  reason: string | null;
  holdings: AlphaHolding[];
  coverage: AlphaCoverage;
  buy_candidates: AlphaBuyCandidate[];
  as_of: string | null;
};

/** The alpha-engine system view joined onto a portfolio's held tickers. */
export const getAlphaEngine = (apiAuth: string, id: string) =>
  get<AlphaEngineView>(apiAuth, `/ext/alpha-engine/${id}`);
