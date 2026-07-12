"use client";

// The import panel: upload a CSV or OFX file, or sync an IBKR Flex query. Each form
// posts to a Server Action (so the tenant header + Flex token stay server-side) and
// shows the result; revalidation refreshes the holdings/income tables on success.

import { useCallback, useEffect, useState, useTransition } from "react";
import {
  addManualPositionAction,
  importCsvAction,
  importOfxAction,
  listSnapTradeConnectionsAction,
  removeSnapTradeConnectionAction,
  setSnapTradeExclusionAction,
  snapTradeConnectUrlAction,
  syncFlexAction,
  syncFlexStoredAction,
  syncSnapTradeAction,
  type ActionResult,
} from "@/app/portfolios/[id]/actions";
import type { SnapTradeConnections } from "@/lib/api";
import { ReadOnlyNotice } from "@/components/ui";
import { isReferencePortfolio } from "@/lib/demo";

type ActionFn = (portfolioId: string, formData: FormData) => Promise<ActionResult>;

function Result({ result }: { result: ActionResult | null }) {
  if (!result) return null;
  return (
    <div className={`mt-2 text-sm ${result.ok ? "text-positive" : "text-negative"}`}>
      {result.message}
      {result.result && result.result.errors.length > 0 ? (
        <ul className="mt-1 list-disc pl-5 text-xs text-muted">
          {result.result.errors.slice(0, 5).map((e) => (
            <li key={e.ref}>
              {e.ref}: {e.reason}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

function FileImport({
  portfolioId,
  action,
  label,
  accept,
}: {
  portfolioId: string;
  action: ActionFn;
  label: string;
  accept: string;
}) {
  const [pending, start] = useTransition();
  const [result, setResult] = useState<ActionResult | null>(null);

  function onSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const formData = new FormData(e.currentTarget);
    start(async () => setResult(await action(portfolioId, formData)));
  }

  return (
    <form onSubmit={onSubmit} className="rounded-lg border border-line p-4">
      <div className="text-sm font-medium">{label}</div>
      <div className="mt-2 flex items-center gap-2">
        <input
          type="file"
          name="file"
          accept={accept}
          required
          className="block w-full text-sm file:mr-3 file:rounded file:border-0 file:bg-white/10 file:px-3 file:py-1.5 file:text-sm"
        />
        <button
          type="submit"
          disabled={pending}
          className="shrink-0 rounded bg-ink px-3 py-1.5 text-sm font-medium text-paper hover:bg-white disabled:opacity-50"
        >
          {pending ? "Importing…" : "Import"}
        </button>
      </div>
      <Result result={result} />
    </form>
  );
}

function FlexImport({ portfolioId, flexStored }: { portfolioId: string; flexStored: boolean }) {
  const [pending, start] = useTransition();
  const [storedPending, startStored] = useTransition();
  const [result, setResult] = useState<ActionResult | null>(null);
  // When the deployment has stored Flex creds, lead with one-click sync and tuck the
  // BYO-token form behind a toggle (still available for a one-off / different query).
  const [showByo, setShowByo] = useState(!flexStored);

  function onSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const formData = new FormData(e.currentTarget);
    start(async () => setResult(await syncFlexAction(portfolioId, formData)));
  }

  return (
    <div className="rounded-lg border border-line p-4">
      <div className="text-sm font-medium">IBKR Flex sync</div>
      {flexStored ? (
        <>
          <p className="mt-1 text-xs text-muted">Syncs your saved Flex query — no token paste.</p>
          <button
            type="button"
            disabled={storedPending}
            onClick={() => startStored(async () => setResult(await syncFlexStoredAction(portfolioId)))}
            className="mt-2 rounded bg-ink px-3 py-1.5 text-sm font-medium text-paper hover:bg-white disabled:opacity-50"
          >
            {storedPending ? "Syncing…" : "Sync IBKR"}
          </button>
          <button
            type="button"
            onClick={() => setShowByo((v) => !v)}
            className="ml-3 text-xs text-muted underline hover:text-ink"
          >
            {showByo ? "Hide one-off token" : "Use a different token"}
          </button>
          <Result result={result} />
        </>
      ) : null}
      {showByo ? (
        <form onSubmit={onSubmit} className={flexStored ? "mt-3 border-t border-line pt-3" : "mt-1"}>
          <p className="text-xs text-muted">Your Flex token is used for one fetch and never stored.</p>
          <div className="mt-2 grid gap-2 sm:grid-cols-2">
        <input
          type="password"
          name="token"
          placeholder="Flex token"
          required
          autoComplete="off"
          className="rounded border border-line px-2 py-1.5 text-sm"
        />
        <input
          type="text"
          name="query_id"
          placeholder="Query id"
          required
          autoComplete="off"
          className="rounded border border-line px-2 py-1.5 text-sm"
        />
      </div>
          <button
            type="submit"
            disabled={pending}
            className="mt-2 rounded bg-ink px-3 py-1.5 text-sm font-medium text-paper hover:bg-white disabled:opacity-50"
          >
            {pending ? "Syncing…" : "Sync Flex"}
          </button>
          {!flexStored ? <Result result={result} /> : null}
        </form>
      ) : null}
    </div>
  );
}

function SnapTradeCard({ portfolioId }: { portfolioId: string }) {
  const [syncPending, startSync] = useTransition();
  const [busy, startBusy] = useTransition(); // connections list + portal link
  const [result, setResult] = useState<ActionResult | null>(null);
  const [conns, setConns] = useState<SnapTradeConnections | null>(null);
  const [connMsg, setConnMsg] = useState<string | null>(null);

  const refresh = useCallback(() => {
    startBusy(async () => {
      const r = await listSnapTradeConnectionsAction(portfolioId);
      if (r.ok && r.data) {
        setConns(r.data);
        setConnMsg(null);
      } else {
        setConns(null);
        setConnMsg(r.message);
      }
    });
  }, [portfolioId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  function link(reconnectId?: string) {
    startBusy(async () => {
      const r = await snapTradeConnectUrlAction(portfolioId, reconnectId);
      if (r.ok && r.url) {
        window.open(r.url, "_blank", "noopener");
        setConnMsg(
          reconnectId
            ? "Finish re-authenticating in the SnapTrade tab, then Refresh."
            : "Finish linking in the SnapTrade tab, then Refresh and Sync.",
        );
      } else {
        setConnMsg(r.message);
      }
    });
  }

  function setExcluded(c: SnapTradeConnections["connections"][number], excluded: boolean) {
    startBusy(async () => {
      const r = await setSnapTradeExclusionAction(portfolioId, c.id, excluded);
      setConnMsg(r.message);
      if (r.ok) {
        const next = await listSnapTradeConnectionsAction(portfolioId);
        if (next.ok && next.data) setConns(next.data);
      }
    });
  }

  function remove(c: SnapTradeConnections["connections"][number]) {
    const ok = window.confirm(
      `Remove the ${c.brokerage || "(unnamed)"} connection from SnapTrade?\n\n` +
        "This frees a SnapTrade plan slot but is irreversible — re-linking later starts a " +
        "brand-new connection. Data already imported into Metron is kept; it just stops refreshing.",
    );
    if (!ok) return;
    startBusy(async () => {
      const r = await removeSnapTradeConnectionAction(portfolioId, c.id);
      setConnMsg(r.message);
      if (r.ok) {
        const next = await listSnapTradeConnectionsAction(portfolioId);
        if (next.ok && next.data) setConns(next.data);
      }
    });
  }

  return (
    <div className="rounded-lg border border-line p-4">
      <div className="text-sm font-medium">SnapTrade</div>
      <p className="mt-1 text-xs text-muted">
        Syncs every linked brokerage. Exclude one only if it&apos;s sourced elsewhere (e.g. IBKR via Flex).
      </p>
      {conns &&
      conns.n_synced_accounts === 0 &&
      conns.connections.some((c) => !c.excluded && !c.disabled && c.n_accounts > 0) ? (
        <div className="mt-2 rounded border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-200">
          A brokerage is linked but its accounts haven&apos;t been imported yet — linking alone doesn&apos;t sync.
          Click <span className="font-medium">Sync</span> below to import them.
        </div>
      ) : null}
      {conns ? (
        <ul className="mt-2 space-y-1 text-xs">
          {conns.connections.length === 0 ? (
            <li className="text-muted">No brokerages linked yet.</li>
          ) : (
            conns.connections.map((c) => (
              <li key={c.id} className="flex items-center justify-between gap-2">
                <span className="font-medium">{c.brokerage || "(unnamed)"}</span>
                <span className="flex items-center gap-2">
                  <span className={c.disabled ? "text-negative" : "text-muted"}>
                    {c.n_accounts} acct{c.n_accounts === 1 ? "" : "s"}
                    {c.disabled ? " · reconnect needed" : ""}
                    {c.excluded ? " · excluded" : ""}
                  </span>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => setExcluded(c, !c.excluded)}
                    className="text-xs underline hover:text-ink disabled:opacity-50"
                    title={
                      c.excluded
                        ? "Include this connection in the sync again"
                        : "Skip this connection on sync — for a broker sourced elsewhere (e.g. IBKR via Flex)"
                    }
                  >
                    {c.excluded ? "Include" : "Exclude"}
                  </button>
                  {c.disabled ? (
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => link(c.id)}
                      className="text-xs underline hover:text-ink disabled:opacity-50"
                      title="Re-authenticate this connection in the SnapTrade portal (keeps its slot)"
                    >
                      Reconnect
                    </button>
                  ) : null}
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => remove(c)}
                    className="text-xs text-negative underline disabled:opacity-50"
                    title="Permanently delete this connection at SnapTrade (frees a plan slot; imported data is kept)"
                  >
                    Remove
                  </button>
                </span>
              </li>
            ))
          )}
        </ul>
      ) : null}
      {connMsg ? <p className="mt-2 text-xs text-muted">{connMsg}</p> : null}
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <button
          type="button"
          disabled={syncPending}
          onClick={() => startSync(async () => setResult(await syncSnapTradeAction(portfolioId)))}
          className="rounded bg-ink px-3 py-1.5 text-sm font-medium text-paper hover:bg-white disabled:opacity-50"
        >
          {syncPending ? "Syncing…" : "Sync"}
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => link()}
          className="rounded border border-line px-3 py-1.5 text-sm font-medium disabled:opacity-50"
          title="Opens the SnapTrade connection portal to link a new brokerage (e.g. E*TRADE) or repair one"
        >
          Link a brokerage…
        </button>
        <button type="button" disabled={busy} onClick={refresh} className="text-xs text-muted hover:text-ink">
          {busy ? "Loading…" : "Refresh"}
        </button>
      </div>
      <Result result={result} />
    </div>
  );
}

function ManualPositionForm({ portfolioId }: { portfolioId: string }) {
  // Stocks/ETFs only (held-away assets — real estate, cash, alts — are explicitly out
  // of scope, tracked separately under the broader Aggregation-breadth work). One
  // ticker + quantity + cost basis synthesizes a single BUY, the same shape a parsed
  // CSV row produces, so it lands through the identical bridge as every other source.
  const [pending, start] = useTransition();
  const [result, setResult] = useState<ActionResult | null>(null);
  const [formKey, setFormKey] = useState(0);

  function onSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = e.currentTarget;
    const formData = new FormData(form);
    start(async () => {
      const r = await addManualPositionAction(portfolioId, formData);
      setResult(r);
      if (r.ok) {
        form.reset();
        setFormKey((k) => k + 1); // remount to clear any uncontrolled state
      }
    });
  }

  return (
    <div className="rounded-lg border border-line p-4">
      <div className="text-sm font-medium">Add position manually</div>
      <p className="mt-1 text-xs text-muted">
        No brokerage connection needed — one stock or ETF at a time (ticker, quantity, cost basis).
      </p>
      <form key={formKey} onSubmit={onSubmit} className="mt-2 grid gap-2 sm:grid-cols-2">
        <input
          type="text"
          name="ticker"
          placeholder="Ticker (e.g. AAPL)"
          required
          autoComplete="off"
          className="rounded border border-line px-2 py-1.5 text-sm uppercase"
        />
        <input
          type="number"
          name="quantity"
          placeholder="Quantity"
          step="any"
          min="0"
          required
          className="rounded border border-line px-2 py-1.5 text-sm"
        />
        <input
          type="number"
          name="cost_basis"
          placeholder="Total cost basis ($)"
          step="any"
          min="0"
          required
          className="rounded border border-line px-2 py-1.5 text-sm"
        />
        <input
          type="date"
          name="trade_date"
          placeholder="Trade date (optional)"
          className="rounded border border-line px-2 py-1.5 text-sm text-muted"
        />
        <button
          type="submit"
          disabled={pending}
          className="sm:col-span-2 rounded bg-ink px-3 py-1.5 text-sm font-medium text-paper hover:bg-white disabled:opacity-50"
        >
          {pending ? "Adding…" : "Add position"}
        </button>
      </form>
      <Result result={result} />
    </div>
  );
}

export function ImportPanel({ portfolioId, flexStored = false }: { portfolioId: string; flexStored?: boolean }) {
  // The Showcase Portfolio (metron-ops#120) is a live, real-tenant-visible read-only
  // mirror (metron#162) — the API 403s every import/sync route for it regardless of caller
  // tenant. Hide the (dead-end) import/connect controls here rather than let a real user
  // click one and get a raw API error.
  if (isReferencePortfolio(portfolioId)) {
    return (
      <div className="rounded-lg border border-dashed border-line p-4">
        <ReadOnlyNotice>Illustrative — read-only. Imports and broker connections aren&apos;t available on this showcase portfolio.</ReadOnlyNotice>
      </div>
    );
  }
  return (
    <div className="grid gap-3 md:grid-cols-2 lg:grid-cols-4">
      <FileImport portfolioId={portfolioId} action={importCsvAction} label="Import CSV" accept=".csv,text/csv" />
      <FileImport
        portfolioId={portfolioId}
        action={importOfxAction}
        label="Import OFX / QFX"
        accept=".ofx,.qfx,application/x-ofx"
      />
      <FlexImport portfolioId={portfolioId} flexStored={flexStored} />
      <SnapTradeCard portfolioId={portfolioId} />
      <ManualPositionForm portfolioId={portfolioId} />
    </div>
  );
}
