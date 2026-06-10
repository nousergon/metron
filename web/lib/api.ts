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
