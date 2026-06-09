"use client";

// The import panel: upload a CSV or OFX file, or sync an IBKR Flex query. Each form
// posts to a Server Action (so the tenant header + Flex token stay server-side) and
// shows the result; revalidation refreshes the holdings/income tables on success.

import { useState, useTransition } from "react";
import { importCsvAction, importOfxAction, syncFlexAction, type ActionResult } from "@/app/portfolios/[id]/actions";

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
          className="block w-full text-sm file:mr-3 file:rounded file:border-0 file:bg-slate-100 file:px-3 file:py-1.5 file:text-sm"
        />
        <button
          type="submit"
          disabled={pending}
          className="shrink-0 rounded bg-ink px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
        >
          {pending ? "Importing…" : "Import"}
        </button>
      </div>
      <Result result={result} />
    </form>
  );
}

function FlexImport({ portfolioId }: { portfolioId: string }) {
  const [pending, start] = useTransition();
  const [result, setResult] = useState<ActionResult | null>(null);

  function onSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const formData = new FormData(e.currentTarget);
    start(async () => setResult(await syncFlexAction(portfolioId, formData)));
  }

  return (
    <form onSubmit={onSubmit} className="rounded-lg border border-line p-4">
      <div className="text-sm font-medium">IBKR Flex sync</div>
      <p className="mt-1 text-xs text-muted">
        Your Flex token is used for one fetch and never stored.
      </p>
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
        className="mt-2 rounded bg-ink px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
      >
        {pending ? "Syncing…" : "Sync Flex"}
      </button>
      <Result result={result} />
    </form>
  );
}

export function ImportPanel({ portfolioId }: { portfolioId: string }) {
  return (
    <div className="grid gap-3 md:grid-cols-3">
      <FileImport portfolioId={portfolioId} action={importCsvAction} label="Import CSV" accept=".csv,text/csv" />
      <FileImport
        portfolioId={portfolioId}
        action={importOfxAction}
        label="Import OFX / QFX"
        accept=".ofx,.qfx,application/x-ofx"
      />
      <FlexImport portfolioId={portfolioId} />
    </div>
  );
}
