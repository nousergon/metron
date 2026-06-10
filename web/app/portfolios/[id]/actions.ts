"use server";

// Server Actions for the import panel. They run server-side, so the tenant header
// (and the user's Flex token) never reach the browser; they forward to the backend
// through the typed client and revalidate the page so the new data shows immediately.

import { revalidatePath } from "next/cache";
import {
  importFile,
  MetronApiError,
  refreshPrices,
  renamePortfolio,
  syncFlex,
  syncSnapTrade,
  type ImportResult,
} from "@/lib/api";
import { requireTenantId } from "@/lib/session";

export type ActionResult = { ok: boolean; message: string; result?: ImportResult };

export async function renamePortfolioAction(portfolioId: string, name: string): Promise<ActionResult> {
  const trimmed = name.trim();
  if (!trimmed) return { ok: false, message: "Name can't be empty." };
  try {
    const tenantId = await requireTenantId();
    await renamePortfolio(tenantId, portfolioId, trimmed);
    revalidatePath(`/portfolios/${portfolioId}`);
    revalidatePath("/");
    return { ok: true, message: "Renamed." };
  } catch (e) {
    return { ok: false, message: e instanceof MetronApiError ? e.message : "Rename failed — backend reachable?" };
  }
}

function summarize(r: ImportResult): string {
  const parts: string[] = [];
  if (r.transactions_inserted) parts.push(`${r.transactions_inserted} transactions`);
  if (r.positions_imported) parts.push(`${r.positions_imported} positions`);
  if (r.transactions_skipped) parts.push(`${r.transactions_skipped} already present`);
  if (r.rows_skipped) parts.push(`${r.rows_skipped} skipped`);
  const summary = parts.length ? parts.join(", ") : "nothing new";
  return `Imported from ${r.source}: ${summary}.`;
}

function errorMessage(e: unknown): string {
  if (e instanceof MetronApiError) return e.message;
  return "Import failed — is the backend reachable?";
}

async function runFileImport(portfolioId: string, kind: "csv" | "ofx", formData: FormData): Promise<ActionResult> {
  const file = formData.get("file");
  if (!(file instanceof File) || file.size === 0) {
    return { ok: false, message: `Choose a ${kind.toUpperCase()} file first.` };
  }
  try {
    const tenantId = await requireTenantId();
    const result = await importFile(tenantId, portfolioId, kind, file);
    revalidatePath(`/portfolios/${portfolioId}`);
    return { ok: true, message: summarize(result), result };
  } catch (e) {
    return { ok: false, message: errorMessage(e) };
  }
}

export async function importCsvAction(portfolioId: string, formData: FormData): Promise<ActionResult> {
  return runFileImport(portfolioId, "csv", formData);
}

export async function importOfxAction(portfolioId: string, formData: FormData): Promise<ActionResult> {
  return runFileImport(portfolioId, "ofx", formData);
}

export async function refreshPricesAction(portfolioId: string): Promise<ActionResult> {
  try {
    const tenantId = await requireTenantId();
    const r = await refreshPrices(tenantId, portfolioId);
    revalidatePath(`/portfolios/${portfolioId}`);
    const msg =
      r.prices_updated > 0
        ? `Priced ${r.prices_updated} of ${r.symbols_requested} holdings.`
        : "No prices found for current holdings.";
    return { ok: true, message: msg };
  } catch (e) {
    return { ok: false, message: e instanceof MetronApiError ? e.message : "Price refresh failed — backend reachable?" };
  }
}

export async function syncSnapTradeAction(portfolioId: string): Promise<ActionResult> {
  try {
    const tenantId = await requireTenantId();
    const result = await syncSnapTrade(tenantId, portfolioId);
    revalidatePath(`/portfolios/${portfolioId}`);
    return { ok: true, message: summarize(result), result };
  } catch (e) {
    return { ok: false, message: errorMessage(e) };
  }
}

export async function syncFlexAction(portfolioId: string, formData: FormData): Promise<ActionResult> {
  const token = String(formData.get("token") ?? "").trim();
  const queryId = String(formData.get("query_id") ?? "").trim();
  if (!token || !queryId) {
    return { ok: false, message: "Both a Flex token and a query id are required." };
  }
  try {
    const tenantId = await requireTenantId();
    const result = await syncFlex(tenantId, portfolioId, token, queryId);
    revalidatePath(`/portfolios/${portfolioId}`);
    return { ok: true, message: summarize(result), result };
  } catch (e) {
    return { ok: false, message: errorMessage(e) };
  }
}
