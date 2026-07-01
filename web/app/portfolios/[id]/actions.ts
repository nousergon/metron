"use server";

// Server Actions for the import panel. They run server-side, so the tenant header
// (and the user's Flex token) never reach the browser; they forward to the backend
// through the typed client and revalidate the page so the new data shows immediately.

import { revalidatePath } from "next/cache";
import {
  type AccountTagPatch,
  addCryptoAddress,
  addWatchlist,
  createSnapTradeConnectUrl,
  deleteAccount,
  deleteCryptoAddress,
  getSnapTradeConnections,
  importFile,
  MetronApiError,
  type HoldingsViewPrefs,
  type Preferences,
  putAccountSelection,
  putHoldingsView,
  putPreferences,
  refreshPrices,
  removeSnapTradeConnection,
  removeWatchlist,
  renamePortfolio,
  restoreExcludedAccount,
  setSecurityClassification,
  setSecurityLabel,
  setSnapTradeConnectionExcluded,
  type SnapTradeConnections,
  syncFlex,
  syncFlexStored,
  syncSnapTrade,
  updateAccountTags,
  updatePortfolio,
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

export async function listSnapTradeConnectionsAction(
  portfolioId: string,
): Promise<{ ok: boolean; message: string; data?: SnapTradeConnections }> {
  try {
    const tenantId = await requireTenantId();
    const data = await getSnapTradeConnections(tenantId, portfolioId);
    return { ok: true, message: "", data };
  } catch (e) {
    if (e instanceof MetronApiError && e.status === 404) {
      return { ok: false, message: "SnapTrade isn't enabled on this deployment." };
    }
    return { ok: false, message: errorMessage(e) };
  }
}

export async function snapTradeConnectUrlAction(
  portfolioId: string,
  reconnectId?: string,
): Promise<{ ok: boolean; message: string; url?: string }> {
  try {
    const tenantId = await requireTenantId();
    const url = await createSnapTradeConnectUrl(tenantId, portfolioId, reconnectId);
    return { ok: true, message: "", url };
  } catch (e) {
    return { ok: false, message: errorMessage(e) };
  }
}

export async function removeSnapTradeConnectionAction(
  portfolioId: string,
  authorizationId: string,
): Promise<{ ok: boolean; message: string }> {
  try {
    const tenantId = await requireTenantId();
    await removeSnapTradeConnection(tenantId, portfolioId, authorizationId);
    return { ok: true, message: "Connection removed — the SnapTrade slot is free." };
  } catch (e) {
    return { ok: false, message: errorMessage(e) };
  }
}

export async function setSnapTradeExclusionAction(
  portfolioId: string,
  authorizationId: string,
  excluded: boolean,
): Promise<{ ok: boolean; message: string }> {
  try {
    const tenantId = await requireTenantId();
    await setSnapTradeConnectionExcluded(tenantId, portfolioId, authorizationId, excluded);
    const message = excluded
      ? "Excluded — future syncs skip this connection (imported data stays)."
      : "Included — the next Sync imports this connection.";
    return { ok: true, message };
  } catch (e) {
    return { ok: false, message: errorMessage(e) };
  }
}

export async function updateBaseCurrencyAction(portfolioId: string, currency: string): Promise<ActionResult> {
  const ccy = currency.trim().toUpperCase();
  if (ccy.length !== 3) return { ok: false, message: "Base currency must be a 3-letter ISO code." };
  try {
    const tenantId = await requireTenantId();
    await updatePortfolio(tenantId, portfolioId, { base_currency: ccy });
    revalidatePath(`/portfolios/${portfolioId}`, "layout");
    return { ok: true, message: `Base currency set to ${ccy}.` };
  } catch (e) {
    return { ok: false, message: e instanceof MetronApiError ? e.message : "Update failed — backend reachable?" };
  }
}

export async function updateAccountTagsAction(
  portfolioId: string,
  accountId: string,
  patch: AccountTagPatch,
): Promise<ActionResult> {
  try {
    const tenantId = await requireTenantId();
    await updateAccountTags(tenantId, portfolioId, accountId, patch);
    revalidatePath(`/portfolios/${portfolioId}`, "layout");
    return { ok: true, message: "Account updated." };
  } catch (e) {
    return { ok: false, message: e instanceof MetronApiError ? e.message : "Update failed — backend reachable?" };
  }
}

export async function deleteAccountAction(portfolioId: string, accountId: string): Promise<ActionResult> {
  try {
    const tenantId = await requireTenantId();
    await deleteAccount(tenantId, portfolioId, accountId);
    revalidatePath(`/portfolios/${portfolioId}`, "layout");
    return { ok: true, message: "Account deleted. Future syncs will skip it (restore from Settings)." };
  } catch (e) {
    return { ok: false, message: e instanceof MetronApiError ? e.message : "Delete failed — backend reachable?" };
  }
}

export async function restoreExcludedAccountAction(portfolioId: string, key: string): Promise<ActionResult> {
  try {
    const tenantId = await requireTenantId();
    await restoreExcludedAccount(tenantId, portfolioId, key);
    revalidatePath(`/portfolios/${portfolioId}`, "layout");
    return { ok: true, message: "Account restored — run a sync to re-import it." };
  } catch (e) {
    return { ok: false, message: e instanceof MetronApiError ? e.message : "Restore failed — backend reachable?" };
  }
}

/** Add a crypto wallet address to track (metron-ops#111). A bad address surfaces the
 * backend's 422 detail (e.g. "not a valid ETH address") inline. */
export async function addCryptoAddressAction(
  portfolioId: string,
  chain: string,
  address: string,
  label?: string,
): Promise<ActionResult> {
  const addr = address.trim();
  if (!addr) return { ok: false, message: "Enter a wallet address." };
  try {
    const tenantId = await requireTenantId();
    await addCryptoAddress(tenantId, portfolioId, chain, addr, label?.trim() || null);
    revalidatePath(`/portfolios/${portfolioId}/crypto`);
    return { ok: true, message: `Tracking ${chain} wallet.` };
  } catch (e) {
    return { ok: false, message: e instanceof MetronApiError ? e.message : "Couldn’t add — backend reachable?" };
  }
}

export async function deleteCryptoAddressAction(portfolioId: string, addressId: string): Promise<ActionResult> {
  try {
    const tenantId = await requireTenantId();
    await deleteCryptoAddress(tenantId, portfolioId, addressId);
    revalidatePath(`/portfolios/${portfolioId}/crypto`);
    return { ok: true, message: "Wallet removed." };
  } catch (e) {
    return { ok: false, message: e instanceof MetronApiError ? e.message : "Remove failed — backend reachable?" };
  }
}

/** Persist the accounts-panel selection. Fire-and-forget from the panel — a save
 * failure must never block the URL-driven filtering, so errors come back as a
 * result (the panel ignores them) rather than throwing. */
export async function saveAccountSelectionAction(portfolioId: string, accountIds: string[]): Promise<ActionResult> {
  try {
    const tenantId = await requireTenantId();
    await putAccountSelection(tenantId, portfolioId, accountIds);
    return { ok: true, message: "Selection saved." };
  } catch (e) {
    return { ok: false, message: e instanceof MetronApiError ? e.message : "Selection save failed." };
  }
}

/** Persist the Holdings-table view (grouping / visible bands / combine) — fire-and-forget
 *  from the toolbar controls so the view survives reloads. No revalidate: the page already
 *  reflects the change client-side. */
export async function saveHoldingsViewAction(portfolioId: string, prefs: HoldingsViewPrefs): Promise<ActionResult> {
  try {
    const tenantId = await requireTenantId();
    await putHoldingsView(tenantId, portfolioId, prefs);
    return { ok: true, message: "View saved." };
  } catch (e) {
    return { ok: false, message: e instanceof MetronApiError ? e.message : "View save failed." };
  }
}

export async function savePreferencesAction(portfolioId: string, prefs: Preferences): Promise<ActionResult> {
  try {
    const tenantId = await requireTenantId();
    await putPreferences(tenantId, portfolioId, prefs);
    revalidatePath(`/portfolios/${portfolioId}/settings`);
    return { ok: true, message: "Preferences saved." };
  } catch (e) {
    return { ok: false, message: e instanceof MetronApiError ? e.message : "Save failed — backend reachable?" };
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

export async function syncFlexStoredAction(portfolioId: string): Promise<ActionResult> {
  try {
    const tenantId = await requireTenantId();
    const result = await syncFlexStored(tenantId, portfolioId);
    revalidatePath(`/portfolios/${portfolioId}`);
    return { ok: true, message: summarize(result), result };
  } catch (e) {
    return { ok: false, message: errorMessage(e) };
  }
}

export async function addWatchlistAction(portfolioId: string, symbol: string, note?: string): Promise<ActionResult> {
  const sym = symbol.trim().toUpperCase();
  if (!sym) return { ok: false, message: "Enter a ticker symbol." };
  try {
    const tenantId = await requireTenantId();
    await addWatchlist(tenantId, portfolioId, sym, note?.trim() || null);
    revalidatePath(`/portfolios/${portfolioId}/watchlist`);
    revalidatePath(`/portfolios/${portfolioId}/holdings`);
    return { ok: true, message: `Added ${sym} to the watchlist.` };
  } catch (e) {
    return { ok: false, message: e instanceof MetronApiError ? e.message : "Couldn’t add — backend reachable?" };
  }
}

export async function removeWatchlistAction(portfolioId: string, symbol: string): Promise<ActionResult> {
  try {
    const tenantId = await requireTenantId();
    await removeWatchlist(tenantId, portfolioId, symbol);
    revalidatePath(`/portfolios/${portfolioId}/watchlist`);
    revalidatePath(`/portfolios/${portfolioId}/holdings`);
    return { ok: true, message: `Removed ${symbol}.` };
  } catch (e) {
    return { ok: false, message: e instanceof MetronApiError ? e.message : "Couldn’t remove — backend reachable?" };
  }
}

export async function setSecurityLabelAction(
  portfolioId: string,
  symbol: string,
  label: string,
): Promise<ActionResult> {
  try {
    const tenantId = await requireTenantId();
    // Empty string clears the alias (reverts to the raw symbol).
    await setSecurityLabel(tenantId, portfolioId, symbol, label.trim() || null);
    revalidatePath(`/portfolios/${portfolioId}`);
    return { ok: true, message: "Label saved." };
  } catch (e) {
    return { ok: false, message: e instanceof MetronApiError ? e.message : "Couldn’t save the label — backend reachable?" };
  }
}

/** Set (or clear) a tenant's sector / country override for a symbol so an Unclassified
 * holding lands in the Allocation breakdown. `field` selects which one; an empty value
 * clears that field. Revalidates the holdings view so the new classification shows. */
export async function setSecurityClassificationAction(
  portfolioId: string,
  symbol: string,
  field: "sector" | "country" | "type",
  value: string,
): Promise<ActionResult> {
  try {
    const tenantId = await requireTenantId();
    // The UI "type" field maps to the API's instrument_type column.
    const key = field === "type" ? "instrument_type" : field;
    // Empty string clears just this field (reverts to the spine/classified value, if any).
    await setSecurityClassification(tenantId, portfolioId, symbol, { [key]: value.trim() || null });
    revalidatePath(`/portfolios/${portfolioId}/holdings`);
    revalidatePath(`/portfolios/${portfolioId}`);
    const label = field === "sector" ? "Sector" : field === "country" ? "Country" : "Type";
    return { ok: true, message: `${label} saved.` };
  } catch (e) {
    return { ok: false, message: e instanceof MetronApiError ? e.message : "Couldn’t save — backend reachable?" };
  }
}
