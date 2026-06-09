// Typed server-side client for the Metron FastAPI backend.
//
// Types mirror the pydantic response models in api/routers/portfolios.py. Calls run
// in Server Components (never the browser), so the placeholder tenant header stays
// server-side until real auth lands (PH4).

const API_URL = process.env.METRON_API_URL ?? "http://localhost:8000";
const DEV_TENANT_ID = process.env.METRON_DEV_TENANT_ID ?? "";

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
  broker: string;
  external_id: string;
  name: string;
  currency: string;
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

/** True when no dev tenant is configured — pages render a setup hint instead of erroring. */
export function tenantConfigured(): boolean {
  return DEV_TENANT_ID.length > 0;
}

async function get<T>(path: string): Promise<T> {
  const headers: Record<string, string> = {};
  if (DEV_TENANT_ID) headers["X-Tenant-Id"] = DEV_TENANT_ID;
  const res = await fetch(`${API_URL}${path}`, { headers, cache: "no-store" });
  if (!res.ok) {
    throw new MetronApiError(res.status, `GET ${path} → ${res.status}`);
  }
  return res.json() as Promise<T>;
}

export const getPortfolios = () => get<Portfolio[]>("/portfolios");
export const getSummary = (id: string) => get<Summary>(`/portfolios/${id}/summary`);
export const getHoldings = (id: string) => get<Holding[]>(`/portfolios/${id}/holdings`);
export const getIncome = (id: string) => get<IncomeYear[]>(`/portfolios/${id}/income`);
export const getAccounts = (id: string) => get<Account[]>(`/portfolios/${id}/accounts`);
