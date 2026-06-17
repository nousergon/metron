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
  avg_cost: number; // native per-share cost
  cost_basis: number; // native total cost basis
  currency: string;
  fx_rate: number | null; // base per 1 unit of `currency` (1.0 for USD)
  // Null until prices are refreshed; populated from the cached EOD close.
  // `_local` fields are native; market_value / cost_basis_base are base-currency.
  last_price: number | null;
  last_price_date: string | null;
  market_value_local: number | null;
  cost_basis_base: number | null;
  market_value: number | null;
  unrealized_gain: number | null;
  unrealized_pct: number | null;
  // Coarse asset class for grouping (cash / bond / equity / etf / fund / option / other).
  security_type: string;
  // User-set display label/alias (so a numeric-CUSIP bond is legible). null when unset.
  user_label: string | null;
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

/** Build the repeatable `?account_id=` selection query (empty when no/zero ids → whole portfolio). */
export function acctParams(accountIds?: string[]): string {
  if (!accountIds || accountIds.length === 0) return "";
  return "?" + accountIds.map((a) => `account_id=${encodeURIComponent(a)}`).join("&");
}

export const getPortfolios = (tenantId: string) => get<Portfolio[]>(tenantId, "/portfolios");
export const getPortfolio = (tenantId: string, id: string) => get<Portfolio>(tenantId, `/portfolios/${id}`);

// Watchlist — tracked tickers (held or not). Read-only/illustrative in the beta: no live
// price (un-held tickers have no price source until the Pro feed). (metron-ops#42)
export type WatchlistEntry = {
  symbol: string;
  name: string | null;
  sector: string | null;
  next_earnings_date: string | null;
  held: boolean;
  note: string | null;
};

export const getWatchlist = (tenantId: string, id: string) =>
  get<WatchlistEntry[]>(tenantId, `/portfolios/${id}/watchlist`);

export async function addWatchlist(
  tenantId: string,
  id: string,
  symbol: string,
  note?: string | null,
): Promise<WatchlistEntry> {
  const res = await fetch(`${API_URL}/portfolios/${id}/watchlist`, {
    method: "POST",
    headers: { "X-Tenant-Id": tenantId, "Content-Type": "application/json" },
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

export async function removeWatchlist(tenantId: string, id: string, symbol: string): Promise<void> {
  const res = await fetch(`${API_URL}/portfolios/${id}/watchlist/${encodeURIComponent(symbol)}`, {
    method: "DELETE",
    headers: { "X-Tenant-Id": tenantId },
    cache: "no-store",
  });
  if (!res.ok) throw new MetronApiError(res.status, `DELETE watchlist → ${res.status}`);
}

/** Set (or clear, with an empty label) a user alias for a symbol (metron-ops#47). */
export async function setSecurityLabel(
  tenantId: string,
  id: string,
  symbol: string,
  label: string | null,
): Promise<{ symbol: string; label: string | null }> {
  const res = await fetch(`${API_URL}/portfolios/${id}/securities/${encodeURIComponent(symbol)}/label`, {
    method: "PUT",
    headers: { "X-Tenant-Id": tenantId, "Content-Type": "application/json" },
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

/** Update a portfolio's name and/or base currency (PATCH). Empty/no-op rejected (422). */
export async function updatePortfolio(
  tenantId: string,
  id: string,
  patch: { name?: string; base_currency?: string },
): Promise<Portfolio> {
  const res = await fetch(`${API_URL}/portfolios/${id}`, {
    method: "PATCH",
    headers: { "X-Tenant-Id": tenantId, "Content-Type": "application/json" },
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
export const renamePortfolio = (tenantId: string, id: string, name: string) =>
  updatePortfolio(tenantId, id, { name });
// The reads below take an optional `accountIds` selection (from the account panel's
// checkboxes); omitted/empty = whole portfolio. `getAccounts` is always unscoped — it
// IS the selector, so it lists every account with its own valuation.
export const getSummary = (tenantId: string, id: string, accountIds?: string[]) =>
  get<Summary>(tenantId, `/portfolios/${id}/summary${acctParams(accountIds)}`);
export const getHoldings = (tenantId: string, id: string, accountIds?: string[]) =>
  get<Holding[]>(tenantId, `/portfolios/${id}/holdings${acctParams(accountIds)}`);
// account selection + the taxable-only flag (the Tax/Activity views default taxable —
// tax-advantaged accounts have no taxable events; metron-ops#48).
function activityQuery(accountIds?: string[], taxableOnly?: boolean): string {
  const parts: string[] = (accountIds ?? []).map((a) => `account_id=${encodeURIComponent(a)}`);
  if (taxableOnly) parts.push("taxable_only=true");
  return parts.length ? "?" + parts.join("&") : "";
}

export const getIncome = (tenantId: string, id: string, accountIds?: string[], taxableOnly?: boolean) =>
  get<IncomeYear[]>(tenantId, `/portfolios/${id}/income${activityQuery(accountIds, taxableOnly)}`);
export const getAccounts = (tenantId: string, id: string) => get<Account[]>(tenantId, `/portfolios/${id}/accounts`);
export const getTransactions = (tenantId: string, id: string, accountIds?: string[], taxableOnly?: boolean) =>
  get<Transaction[]>(tenantId, `/portfolios/${id}/transactions${activityQuery(accountIds, taxableOnly)}`);
export const getRealized = (tenantId: string, id: string, accountIds?: string[], taxableOnly?: boolean) =>
  get<RealizedLot[]>(tenantId, `/portfolios/${id}/realized${activityQuery(accountIds, taxableOnly)}`);
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

export const getRisk = (tenantId: string, id: string, accountIds?: string[]) =>
  get<Risk>(tenantId, `/portfolios/${id}/risk${acctParams(accountIds)}`);

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
  harvestable_loss: number | null;
  n_accounts_excluded: number;
  lots: TaxLot[];
};

export const getTax = (tenantId: string, id: string, accountIds?: string[]) =>
  get<Tax>(tenantId, `/portfolios/${id}/tax${acctParams(accountIds)}`);

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

export const getAttribution = (tenantId: string, id: string, accountIds?: string[]) =>
  get<Attribution>(tenantId, `/portfolios/${id}/attribution${acctParams(accountIds)}`);

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

/** Resolve sectors + backfill history, then run Brinson attribution (heavier POST).
 * `accountIds` scopes the computation to the selected accounts. */
export async function computeAttribution(tenantId: string, id: string, accountIds?: string[]): Promise<Attribution> {
  const res = await fetch(`${API_URL}/portfolios/${id}/attribution/compute${acctParams(accountIds)}`, {
    method: "POST",
    headers: { "X-Tenant-Id": tenantId },
    cache: "no-store",
  });
  if (!res.ok) {
    throw new MetronApiError(res.status, `compute attribution → ${res.status}`);
  }
  return res.json() as Promise<Attribution>;
}

/** Backfill history + compute factor risk (the heavier POST path). `accountIds` scopes it. */
export async function computeRisk(tenantId: string, id: string, accountIds?: string[]): Promise<Risk> {
  const res = await fetch(`${API_URL}/portfolios/${id}/risk/compute${acctParams(accountIds)}`, {
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

/** Sync the operator's linked SnapTrade brokerages into a portfolio (every linked
 * connection, minus any the portfolio has excluded). Personal/single-operator only —
 * 404 when the deployment hasn't enabled it. */
export async function syncSnapTrade(tenantId: string, id: string): Promise<ImportResult> {
  const res = await fetch(`${API_URL}/portfolios/${id}/import/snaptrade`, {
    method: "POST",
    headers: { "X-Tenant-Id": tenantId },
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
};

export const getSnapTradeConnections = (tenantId: string, id: string) =>
  get<SnapTradeConnections>(tenantId, `/portfolios/${id}/snaptrade/connections`);

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
  tenantId: string,
  id: string,
  reconnectId?: string,
): Promise<string> {
  const res = await fetch(`${API_URL}/portfolios/${id}/snaptrade/connect`, {
    method: "POST",
    headers: { "X-Tenant-Id": tenantId, "Content-Type": "application/json" },
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
  tenantId: string,
  id: string,
  authorizationId: string,
): Promise<void> {
  const res = await fetch(
    `${API_URL}/portfolios/${id}/snaptrade/connections/${encodeURIComponent(authorizationId)}`,
    {
      method: "DELETE",
      headers: { "X-Tenant-Id": tenantId },
      cache: "no-store",
    },
  );
  if (!res.ok) throw await readDetailError(res, "SnapTrade remove");
}

/** Toggle a connection's sync opt-out (exclude=true skips it on future syncs;
 * already-imported data stays). Keyed by stable connection id — no name matching. */
export async function setSnapTradeConnectionExcluded(
  tenantId: string,
  id: string,
  authorizationId: string,
  excluded: boolean,
): Promise<void> {
  const verb = excluded ? "exclude" : "include";
  const res = await fetch(
    `${API_URL}/portfolios/${id}/snaptrade/connections/${encodeURIComponent(authorizationId)}/${verb}`,
    {
      method: "POST",
      headers: { "X-Tenant-Id": tenantId },
      cache: "no-store",
    },
  );
  if (!res.ok) throw await readDetailError(res, `SnapTrade ${verb}`);
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
  tenantId: string,
  id: string,
  accountId: string,
  patch: AccountTagPatch,
): Promise<Account> {
  const res = await fetch(`${API_URL}/portfolios/${id}/accounts/${accountId}`, {
    method: "PATCH",
    headers: { "X-Tenant-Id": tenantId, "Content-Type": "application/json" },
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
  tenantId: string,
  id: string,
  accountId: string,
): Promise<{ account_id: string; excluded_key: string }> {
  const res = await fetch(`${API_URL}/portfolios/${id}/accounts/${accountId}`, {
    method: "DELETE",
    headers: { "X-Tenant-Id": tenantId },
    cache: "no-store",
  });
  if (!res.ok) {
    throw new MetronApiError(res.status, `delete account → ${res.status}`);
  }
  return res.json();
}

export type ExcludedAccount = { key: string; broker: string; external_id: string };

/** Deleted broker accounts (imports skip these keys) — restorable from Settings. */
export const getExcludedAccounts = (tenantId: string, id: string) =>
  get<{ excluded: ExcludedAccount[] }>(tenantId, `/portfolios/${id}/accounts/excluded`);

/** Drop a key from the exclusion list — the next sync re-imports that account. */
export async function restoreExcludedAccount(
  tenantId: string,
  id: string,
  key: string,
): Promise<{ excluded: ExcludedAccount[] }> {
  const res = await fetch(`${API_URL}/portfolios/${id}/accounts/excluded/restore`, {
    method: "POST",
    headers: { "X-Tenant-Id": tenantId, "Content-Type": "application/json" },
    body: JSON.stringify({ key }),
    cache: "no-store",
  });
  if (!res.ok) {
    throw new MetronApiError(res.status, `restore account → ${res.status}`);
  }
  return res.json();
}

/** The saved accounts-panel selection (empty = whole portfolio). */
export const getAccountSelection = async (tenantId: string, id: string): Promise<string[]> =>
  (await get<{ account_ids: string[] }>(tenantId, `/portfolios/${id}/accounts/selection`)).account_ids;

/** Save the accounts-panel selection (empty list clears it = whole portfolio). */
export async function putAccountSelection(
  tenantId: string,
  id: string,
  accountIds: string[],
): Promise<void> {
  const res = await fetch(`${API_URL}/portfolios/${id}/accounts/selection`, {
    method: "PUT",
    headers: { "X-Tenant-Id": tenantId, "Content-Type": "application/json" },
    body: JSON.stringify({ account_ids: accountIds }),
    cache: "no-store",
  });
  if (!res.ok) {
    throw new MetronApiError(res.status, `save selection → ${res.status}`);
  }
}

export type Preferences = {
  risk_tolerance: string | null;
  objective: string | null;
  notes: string | null;
};

export const getPreferences = (tenantId: string, id: string) =>
  get<Preferences>(tenantId, `/portfolios/${id}/preferences`);

/** Create or update the portfolio's investor preferences (PUT, idempotent). */
export async function putPreferences(
  tenantId: string,
  id: string,
  prefs: Preferences,
): Promise<Preferences> {
  const res = await fetch(`${API_URL}/portfolios/${id}/preferences`, {
    method: "PUT",
    headers: { "X-Tenant-Id": tenantId, "Content-Type": "application/json" },
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
// plugins are installed, so `getPlugins` returns [] and none of the advisor
// surface below is ever reached. Types mirror the metron-ops advisor router.
// ---------------------------------------------------------------------------

/** Nav metadata for one active out-of-tree plugin (GET /meta/plugins). */
export type PluginNav = { id: string; label: string; href: string; tier: string };

/** Active premium plugins for this deploy (empty on the public tier). */
export const getPlugins = (tenantId: string) => get<PluginNav[]>(tenantId, "/meta/plugins");

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
  tenantId: string,
  preview?: { tier?: string; feed?: boolean },
) => {
  const params = new URLSearchParams();
  if (preview?.tier) params.set("preview_tier", preview.tier);
  if (preview?.feed !== undefined) params.set("preview_feed", String(preview.feed));
  const qs = params.toString();
  return get<Entitlements>(tenantId, `/meta/entitlements${qs ? `?${qs}` : ""}`);
};

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
