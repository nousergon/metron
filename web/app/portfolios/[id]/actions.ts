"use server";

// Server Actions for the import panel. They run server-side, so the tenant header
// (and the user's Flex token) never reach the browser; they forward to the backend
// through the typed client and revalidate the page so the new data shows immediately.

import { revalidatePath, revalidateTag } from "next/cache";
import { accountsMetaTag } from "@/lib/account-meta";
import {
  type AccountTagPatch,
  addCryptoAddress,
  addManualPosition,
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
import { requireApiAuth } from "@/lib/session";

export type ActionResult = { ok: boolean; message: string; result?: ImportResult };

export async function renamePortfolioAction(portfolioId: string, name: string): Promise<ActionResult> {
  const trimmed = name.trim();
  if (!trimmed) return { ok: false, message: "Name can't be empty." };
  try {
    const apiAuth = await requireApiAuth();
    await renamePortfolio(apiAuth, portfolioId, trimmed);
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
    const apiAuth = await requireApiAuth();
    const result = await importFile(apiAuth, portfolioId, kind, file);
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

/** Add a single manually-entered stock/ETF position (metron-ops#187) — the no-brokerage,
 * no-file path alongside CSV/OFX import. Validates the required fields client-visibly
 * before the request; ticker/quantity validity beyond that (e.g. an unrecognizable
 * symbol) is enforced server-side and surfaced via the backend's 422 detail. */
export async function addManualPositionAction(portfolioId: string, formData: FormData): Promise<ActionResult> {
  const ticker = String(formData.get("ticker") ?? "").trim();
  const quantityRaw = String(formData.get("quantity") ?? "").trim();
  const costBasisRaw = String(formData.get("cost_basis") ?? "").trim();
  const tradeDate = String(formData.get("trade_date") ?? "").trim();

  if (!ticker) return { ok: false, message: "Enter a ticker symbol." };
  const quantity = Number(quantityRaw);
  if (!quantityRaw || !Number.isFinite(quantity) || quantity <= 0) {
    return { ok: false, message: "Quantity must be a positive number." };
  }
  const costBasis = Number(costBasisRaw);
  if (!costBasisRaw || !Number.isFinite(costBasis) || costBasis < 0) {
    return { ok: false, message: "Cost basis must be zero or a positive number." };
  }

  try {
    const apiAuth = await requireApiAuth();
    const result = await addManualPosition(apiAuth, portfolioId, {
      ticker,
      quantity,
      costBasis,
      tradeDate: tradeDate || null,
    });
    revalidatePath(`/portfolios/${portfolioId}`);
    return { ok: true, message: `Added ${ticker.toUpperCase()} — ${summarize(result)}`, result };
  } catch (e) {
    return { ok: false, message: errorMessage(e) };
  }
}

export async function refreshPricesAction(portfolioId: string): Promise<ActionResult> {
  try {
    const apiAuth = await requireApiAuth();
    const r = await refreshPrices(apiAuth, portfolioId);
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
    const apiAuth = await requireApiAuth();
    const result = await syncSnapTrade(apiAuth, portfolioId);
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
    const apiAuth = await requireApiAuth();
    const data = await getSnapTradeConnections(apiAuth, portfolioId);
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
    const apiAuth = await requireApiAuth();
    const url = await createSnapTradeConnectUrl(apiAuth, portfolioId, reconnectId);
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
    const apiAuth = await requireApiAuth();
    await removeSnapTradeConnection(apiAuth, portfolioId, authorizationId);
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
    const apiAuth = await requireApiAuth();
    await setSnapTradeConnectionExcluded(apiAuth, portfolioId, authorizationId, excluded);
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
    const apiAuth = await requireApiAuth();
    await updatePortfolio(apiAuth, portfolioId, { base_currency: ccy });
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
    const apiAuth = await requireApiAuth();
    await updateAccountTags(apiAuth, portfolioId, accountId, patch);
    revalidatePath(`/portfolios/${portfolioId}`, "layout");
    revalidateTag(accountsMetaTag(apiAuth, portfolioId));
    return { ok: true, message: "Account updated." };
  } catch (e) {
    return { ok: false, message: e instanceof MetronApiError ? e.message : "Update failed — backend reachable?" };
  }
}

export async function deleteAccountAction(portfolioId: string, accountId: string): Promise<ActionResult> {
  try {
    const apiAuth = await requireApiAuth();
    await deleteAccount(apiAuth, portfolioId, accountId);
    revalidatePath(`/portfolios/${portfolioId}`, "layout");
    revalidateTag(accountsMetaTag(apiAuth, portfolioId));
    return { ok: true, message: "Account deleted. Future syncs will skip it (restore from Settings)." };
  } catch (e) {
    return { ok: false, message: e instanceof MetronApiError ? e.message : "Delete failed — backend reachable?" };
  }
}

export async function restoreExcludedAccountAction(portfolioId: string, key: string): Promise<ActionResult> {
  try {
    const apiAuth = await requireApiAuth();
    await restoreExcludedAccount(apiAuth, portfolioId, key);
    revalidatePath(`/portfolios/${portfolioId}`, "layout");
    revalidateTag(accountsMetaTag(apiAuth, portfolioId));
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
    const apiAuth = await requireApiAuth();
    await addCryptoAddress(apiAuth, portfolioId, chain, addr, label?.trim() || null);
    revalidatePath(`/portfolios/${portfolioId}/crypto`);
    return { ok: true, message: `Tracking ${chain} wallet.` };
  } catch (e) {
    return { ok: false, message: e instanceof MetronApiError ? e.message : "Couldn’t add — backend reachable?" };
  }
}

export async function deleteCryptoAddressAction(portfolioId: string, addressId: string): Promise<ActionResult> {
  try {
    const apiAuth = await requireApiAuth();
    await deleteCryptoAddress(apiAuth, portfolioId, addressId);
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
    const apiAuth = await requireApiAuth();
    await putAccountSelection(apiAuth, portfolioId, accountIds);
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
    const apiAuth = await requireApiAuth();
    await putHoldingsView(apiAuth, portfolioId, prefs);
    return { ok: true, message: "View saved." };
  } catch (e) {
    return { ok: false, message: e instanceof MetronApiError ? e.message : "View save failed." };
  }
}

export async function savePreferencesAction(portfolioId: string, prefs: Preferences): Promise<ActionResult> {
  try {
    const apiAuth = await requireApiAuth();
    await putPreferences(apiAuth, portfolioId, prefs);
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
    const apiAuth = await requireApiAuth();
    const result = await syncFlex(apiAuth, portfolioId, token, queryId);
    revalidatePath(`/portfolios/${portfolioId}`);
    return { ok: true, message: summarize(result), result };
  } catch (e) {
    return { ok: false, message: errorMessage(e) };
  }
}

export async function syncFlexStoredAction(portfolioId: string): Promise<ActionResult> {
  try {
    const apiAuth = await requireApiAuth();
    const result = await syncFlexStored(apiAuth, portfolioId);
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
    const apiAuth = await requireApiAuth();
    await addWatchlist(apiAuth, portfolioId, sym, note?.trim() || null);
    revalidatePath(`/portfolios/${portfolioId}/watchlist`);
    revalidatePath(`/portfolios/${portfolioId}`);
    return { ok: true, message: `Added ${sym} to the watchlist.` };
  } catch (e) {
    return { ok: false, message: e instanceof MetronApiError ? e.message : "Couldn’t add — backend reachable?" };
  }
}

export async function removeWatchlistAction(portfolioId: string, symbol: string): Promise<ActionResult> {
  try {
    const apiAuth = await requireApiAuth();
    await removeWatchlist(apiAuth, portfolioId, symbol);
    revalidatePath(`/portfolios/${portfolioId}/watchlist`);
    revalidatePath(`/portfolios/${portfolioId}`);
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
    const apiAuth = await requireApiAuth();
    // Empty string clears the alias (reverts to the raw symbol).
    await setSecurityLabel(apiAuth, portfolioId, symbol, label.trim() || null);
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
    const apiAuth = await requireApiAuth();
    // Empty string clears just this field (reverts to the spine/classified value, if any).
    const trimmed = value.trim() || null;
    // Server Actions are callable directly (not just from the compiled UI), so `field`
    // is only TS-narrowed at compile time, not at runtime — branch explicitly per field
    // instead of building the patch with a computed key derived from it (CodeQL
    // js/remote-property-injection, metron#33 / config#2610).
    let label: string;
    switch (field) {
      case "sector":
        await setSecurityClassification(apiAuth, portfolioId, symbol, { sector: trimmed });
        label = "Sector";
        break;
      case "country":
        await setSecurityClassification(apiAuth, portfolioId, symbol, { country: trimmed });
        label = "Country";
        break;
      case "type":
        await setSecurityClassification(apiAuth, portfolioId, symbol, { instrument_type: trimmed });
        label = "Type";
        break;
      default:
        return { ok: false, message: "Invalid classification field." };
    }
    revalidatePath(`/portfolios/${portfolioId}`);
    return { ok: true, message: `${label} saved.` };
  } catch (e) {
    return { ok: false, message: e instanceof MetronApiError ? e.message : "Couldn’t save — backend reachable?" };
  }
}
