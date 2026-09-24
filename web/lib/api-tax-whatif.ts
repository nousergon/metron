// Typed server-side client for the "if sold" tax preview (metron-ops#208): tax math on a
// hypothetical sale the user authors — holding, quantity, price and lot method are all the
// user's inputs. Read-only GET; the backend builds the ledger in memory and writes nothing.

import { apiFetch, authHeaders, MetronApiError } from "@/lib/api";

export { MetronApiError };

export type LotMethod = "fifo" | "specific";

export type IfSoldOpenLot = {
  lot_index: number;
  open_date: string;
  quantity: number;
  cost_per_share: number;
  cost_basis: number;
  holding_days: number;
  term: string;
};

export type IfSoldSoldLot = IfSoldOpenLot & {
  proceeds: number;
  gain: number;
};

export type IfSoldSale = {
  method: LotMethod;
  quantity: number;
  price: number;
  proceeds: number;
  cost_basis: number;
  gain_st: number;
  gain_lt: number;
  gain_total: number;
  taxable_st: number;
  taxable_lt: number;
  est_tax_st: number;
  est_tax_lt: number;
  est_tax_state: number;
  est_tax_total: number;
  lots: IfSoldSoldLot[];
};

export type IfSoldDelta = {
  cost_basis: number;
  gain_st: number;
  gain_lt: number;
  gain_total: number;
  est_tax_total: number;
};

export type IfSoldRates = { short_term: number; long_term: number; state: number };

export type IfSoldPreview = {
  as_of: string;
  ticker: string;
  currency: string;
  price: number;
  price_source: "latest_close" | "user";
  price_as_of: string | null;
  quantity_held: number;
  rates: IfSoldRates;
  /** Names of the rates that fell back to the backend's placeholders (not the user's). */
  rates_placeholder: (keyof IfSoldRates)[];
  open_lots: IfSoldOpenLot[];
  sale: IfSoldSale;
  fifo: IfSoldSale;
  delta_vs_fifo: IfSoldDelta | null;
  n_accounts_excluded: number;
  history_incomplete: boolean;
};

export type IfSoldInput = {
  ticker: string;
  /** Omitted → the full position. */
  quantity?: number;
  /** Omitted → the latest cached close. */
  price?: number;
  method?: LotMethod;
  /** Specific-lot picks: lot_index → quantity. */
  lots?: { lot_index: number; quantity: number }[];
  /** Fractions (0.24 = 24%). Omitted → a labelled placeholder. */
  st_rate?: number;
  lt_rate?: number;
  state_rate?: number;
  accountIds?: string[];
};

/** Build the `/tax/if-sold` query path. Every value is individually encoded. */
export function ifSoldPath(portfolioId: string, input: IfSoldInput): string {
  const qs = new URLSearchParams();
  qs.set("ticker", input.ticker.trim());
  if (input.quantity != null) qs.set("quantity", String(input.quantity));
  if (input.price != null) qs.set("price", String(input.price));
  if (input.method) qs.set("method", input.method);
  for (const pick of input.lots ?? []) qs.append("lot", `${pick.lot_index}:${pick.quantity}`);
  if (input.st_rate != null) qs.set("st_rate", String(input.st_rate));
  if (input.lt_rate != null) qs.set("lt_rate", String(input.lt_rate));
  if (input.state_rate != null) qs.set("state_rate", String(input.state_rate));
  for (const id of input.accountIds ?? []) qs.append("account_id", id);
  return `/portfolios/${encodeURIComponent(portfolioId)}/tax/if-sold?${qs.toString()}`;
}

export async function getIfSold(apiAuth: string, portfolioId: string, input: IfSoldInput): Promise<IfSoldPreview> {
  const path = ifSoldPath(portfolioId, input);
  const res = await apiFetch(path, { headers: authHeaders(apiAuth), cache: "no-store" });
  if (!res.ok) {
    let detail = `GET ${path} → ${res.status}`;
    try {
      const body = (await res.json()) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      // keep the status-line fallback
    }
    throw new MetronApiError(res.status, detail);
  }
  return res.json() as Promise<IfSoldPreview>;
}
