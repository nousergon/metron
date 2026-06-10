// Typed server-side client for the Metron FastAPI backend.
//
// Types mirror the pydantic response models in api/routers/portfolios.py. Calls run
// in Server Components / Server Actions (never the browser); the caller resolves the
// tenant id from the auth session and passes it in, so the X-Tenant-Id header is
// always server-side and scoped to the signed-in user's workspace.

const API_URL = process.env.METRON_API_URL ?? "http://localhost:8000";

export type Portfolio = { id: string; name: string; base_currency: string };

export type Holding = {
  ticker: string;
  quantity: number;
  avg_cost: number;
  cost_basis: number;
  // Null until prices are refreshed; populated from the cached EOD close.
  last_price: number | null;
  last_price_date: string | null;
  market_value: number | null;
  unrealized_gain: number | null;
  unrealized_pct: number | null;
};

export type IncomeYear = {
  year: number;
  realized_st: number;
  realized_lt: number;
  dividends: number;
  interest: number;
  net_capital_gains: number;
  taxable_income: number;
};

export type Account = {
  account_id: string;
  broker: string;
  external_id: string;
  name: string;
  currency: string;
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
  amount: number;
  fees: number;
  currency: string;
};

export type RealizedLot = {
  ticker: string;
  open_date: string; // ISO date
  close_date: string; // ISO date
  quantity: number;
  proceeds: number;
  cost_basis: number;
  gain: number;
  long_term: boolean;
};

export type Summary = {
  base_currency: string;
  n_accounts: number;
  n_holdings: number;
  total_cost_basis: number;
  realized_st: number;
  realized_lt: number;
  realized_total: number;
  dividends: number;
  interest: number;
  taxable_income: number;
  market_value: number | null;
  unrealized_gain: number | null;
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
  spy_return: number | null;
  alpha: number | null;
  points: PerfPoint[];
};

export class MetronApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

async function get<T>(tenantId: string, path: string): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, {
    headers: { "X-Tenant-Id": tenantId },
    cache: "no-store",
  });
  if (!res.ok) {
    throw new MetronApiError(res.status, `GET ${path} → ${res.status}`);
  }
  return res.json() as Promise<T>;
}

export const getPortfolios = (tenantId: string) => get<Portfolio[]>(tenantId, "/portfolios");
export const getPortfolio = (tenantId: string, id: string) => get<Portfolio>(tenantId, `/portfolios/${id}`);

/** Rename a portfolio (PATCH). Empty names are rejected by the backend (422). */
export async function renamePortfolio(tenantId: string, id: string, name: string): Promise<Portfolio> {
  const res = await fetch(`${API_URL}/portfolios/${id}`, {
    method: "PATCH",
    headers: { "X-Tenant-Id": tenantId, "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
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
export const getSummary = (tenantId: string, id: string) => get<Summary>(tenantId, `/portfolios/${id}/summary`);
export const getHoldings = (tenantId: string, id: string) => get<Holding[]>(tenantId, `/portfolios/${id}/holdings`);
export const getIncome = (tenantId: string, id: string) => get<IncomeYear[]>(tenantId, `/portfolios/${id}/income`);
export const getAccounts = (tenantId: string, id: string) => get<Account[]>(tenantId, `/portfolios/${id}/accounts`);
export const getTransactions = (tenantId: string, id: string) =>
  get<Transaction[]>(tenantId, `/portfolios/${id}/transactions`);
export const getRealized = (tenantId: string, id: string) =>
  get<RealizedLot[]>(tenantId, `/portfolios/${id}/realized`);
export const getAccountDetail = (tenantId: string, id: string, accountId: string) =>
  get<AccountDetail>(tenantId, `/portfolios/${id}/accounts/${accountId}`);
export const getPerformance = (tenantId: string, id: string) =>
  get<Performance>(tenantId, `/portfolios/${id}/performance`);

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

export const getRisk = (tenantId: string, id: string) => get<Risk>(tenantId, `/portfolios/${id}/risk`);

export type TaxLot = {
  ticker: string;
  open_date: string;
  quantity: number;
  cost_basis: number;
  term: string;
  market_value: number | null;
  unrealized_gain: number | null;
  harvestable_loss: number;
};

export type Tax = {
  as_of: string;
  n_lots: number;
  n_priced: number;
  unrealized_st: number | null;
  unrealized_lt: number | null;
  unrealized_total: number | null;
  harvestable_loss: number;
  lots: TaxLot[];
};

export const getTax = (tenantId: string, id: string) => get<Tax>(tenantId, `/portfolios/${id}/tax`);

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

export const getAttribution = (tenantId: string, id: string) =>
  get<Attribution>(tenantId, `/portfolios/${id}/attribution`);

export type MacroPoint = { obs_date: string; value: number };

export type MacroIndicator = {
  key: string;
  label: string;
  units: string;
  latest_value: number;
  latest_date: string;
  prior_value: number | null;
  change: number | null;
  history: MacroPoint[];
};

export type Macro = {
  available: boolean;
  reason: string | null;
  as_of: string | null;
  indicators: MacroIndicator[];
};

// Macro is global market data; the tenant header is sent for client consistency but
// the endpoint ignores it.
export const getMacro = (tenantId: string) => get<Macro>(tenantId, `/macro`);

export type CalendarEvent = { event_date: string; kind: string; ticker: string; label: string };

export type Calendar = {
  as_of: string;
  horizon_days: number;
  n_events: number;
  events: CalendarEvent[];
};

export const getCalendar = (tenantId: string, id: string) =>
  get<Calendar>(tenantId, `/portfolios/${id}/calendar`);

/** Refresh held-ticker earnings dates (yfinance), then return the calendar (heavier POST). */
export async function refreshCalendar(tenantId: string, id: string): Promise<Calendar> {
  const res = await fetch(`${API_URL}/portfolios/${id}/calendar/refresh`, {
    method: "POST",
    headers: { "X-Tenant-Id": tenantId },
    cache: "no-store",
  });
  if (!res.ok) {
    throw new MetronApiError(res.status, `refresh calendar → ${res.status}`);
  }
  return res.json() as Promise<Calendar>;
}

/** Resolve sectors + backfill history, then run Brinson attribution (heavier POST). */
export async function computeAttribution(tenantId: string, id: string): Promise<Attribution> {
  const res = await fetch(`${API_URL}/portfolios/${id}/attribution/compute`, {
    method: "POST",
    headers: { "X-Tenant-Id": tenantId },
    cache: "no-store",
  });
  if (!res.ok) {
    throw new MetronApiError(res.status, `compute attribution → ${res.status}`);
  }
  return res.json() as Promise<Attribution>;
}

/** Backfill history + compute factor risk (the heavier POST path). */
export async function computeRisk(tenantId: string, id: string): Promise<Risk> {
  const res = await fetch(`${API_URL}/portfolios/${id}/risk/compute`, {
    method: "POST",
    headers: { "X-Tenant-Id": tenantId },
    cache: "no-store",
  });
  if (!res.ok) {
    throw new MetronApiError(res.status, `compute risk → ${res.status}`);
  }
  return res.json() as Promise<Risk>;
}

/** Seed NAV history from past prices (reconstruction). Returns the populated summary. */
export async function reconstructPerformance(tenantId: string, id: string): Promise<Performance> {
  const res = await fetch(`${API_URL}/portfolios/${id}/performance/reconstruct`, {
    method: "POST",
    headers: { "X-Tenant-Id": tenantId },
    cache: "no-store",
  });
  if (!res.ok) {
    throw new MetronApiError(res.status, `reconstruct → ${res.status}`);
  }
  return res.json() as Promise<Performance>;
}

/** Create a portfolio in the user's workspace (auto-provisions the tenant on the backend). */
export async function createPortfolio(tenantId: string, name: string): Promise<Portfolio> {
  const res = await fetch(`${API_URL}/portfolios`, {
    method: "POST",
    headers: { "X-Tenant-Id": tenantId, "Content-Type": "application/json" },
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
  tenantId: string,
  id: string,
  kind: "csv" | "ofx",
  file: File,
): Promise<ImportResult> {
  const form = new FormData();
  form.append("file", file, file.name);
  const res = await fetch(`${API_URL}/portfolios/${id}/import/${kind}`, {
    method: "POST",
    headers: { "X-Tenant-Id": tenantId },
    body: form,
    cache: "no-store",
  });
  return readResult(res, `${kind.toUpperCase()} import`);
}

export async function syncFlex(tenantId: string, id: string, token: string, queryId: string): Promise<ImportResult> {
  const res = await fetch(`${API_URL}/portfolios/${id}/import/flex`, {
    method: "POST",
    headers: { "X-Tenant-Id": tenantId, "Content-Type": "application/json" },
    body: JSON.stringify({ token, query_id: queryId }),
    cache: "no-store",
  });
  return readResult(res, "IBKR Flex sync");
}

/** Refresh the EOD price cache for a portfolio's held tickers (market value follows). */
export async function refreshPrices(tenantId: string, id: string): Promise<PriceRefreshResult> {
  const res = await fetch(`${API_URL}/portfolios/${id}/prices/refresh`, {
    method: "POST",
    headers: { "X-Tenant-Id": tenantId },
    cache: "no-store",
  });
  if (!res.ok) {
    throw new MetronApiError(res.status, `price refresh → ${res.status}`);
  }
  return res.json() as Promise<PriceRefreshResult>;
}

// ---------------------------------------------------------------------------
// Extension point — premium plugins (metron-ops). On a stock public deploy no
// plugins are installed, so `getPlugins` returns [] and none of the advisor
// surface below is ever reached. Types mirror the metron-ops advisor router.
// ---------------------------------------------------------------------------

/** Nav metadata for one active out-of-tree plugin (GET /meta/plugins). */
export type PluginNav = { id: string; label: string; href: string; tier: string };

/** Active premium plugins for this deploy (empty on the public tier). */
export const getPlugins = (tenantId: string) => get<PluginNav[]>(tenantId, "/meta/plugins");

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

/** The advisor view (gap analysis + cached commentary) for a portfolio. */
export const getAdvisor = (tenantId: string, id: string) =>
  get<AdvisorView>(tenantId, `/ext/advisor/${id}`);

export const getAdvisorProfile = (tenantId: string, id: string) =>
  get<AdvisorProfile>(tenantId, `/ext/advisor/${id}/profile`);

/** Run the Claude narrative for the current state (the one paid path). */
export async function generateAdvisor(tenantId: string, id: string): Promise<AdvisorView> {
  const res = await fetch(`${API_URL}/ext/advisor/${id}/generate`, {
    method: "POST",
    headers: { "X-Tenant-Id": tenantId },
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

/** Save the tenant's investor profile (the targets the advisor compares against). */
export async function putAdvisorProfile(
  tenantId: string,
  id: string,
  profile: AdvisorProfile,
): Promise<AdvisorProfile> {
  const res = await fetch(`${API_URL}/ext/advisor/${id}/profile`, {
    method: "PUT",
    headers: { "X-Tenant-Id": tenantId, "Content-Type": "application/json" },
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
export const getAlphaEngine = (tenantId: string, id: string) =>
  get<AlphaEngineView>(tenantId, `/ext/alpha-engine/${id}`);
