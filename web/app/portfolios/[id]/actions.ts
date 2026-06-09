"use server";

// Server Actions for the import panel. They run server-side, so the tenant header
// (and the user's Flex token) never reach the browser; they forward to the backend
// through the typed client and revalidate the page so the new data shows immediately.

import { revalidatePath } from "next/cache";
import { importFile, MetronApiError, syncFlex, type ImportResult } from "@/lib/api";

export type ActionResult = { ok: boolean; message: string; result?: ImportResult };

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
    const result = await importFile(portfolioId, kind, file);
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

export async function syncFlexAction(portfolioId: string, formData: FormData): Promise<ActionResult> {
  const token = String(formData.get("token") ?? "").trim();
  const queryId = String(formData.get("query_id") ?? "").trim();
  if (!token || !queryId) {
    return { ok: false, message: "Both a Flex token and a query id are required." };
  }
  try {
    const result = await syncFlex(portfolioId, token, queryId);
    revalidatePath(`/portfolios/${portfolioId}`);
    return { ok: true, message: summarize(result), result };
  } catch (e) {
    return { ok: false, message: errorMessage(e) };
  }
}
