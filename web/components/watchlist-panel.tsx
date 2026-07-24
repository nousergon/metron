"use client";

// Watchlist (metron-ops#42) — add/remove tracked tickers and see reference data + a held
// flag. Read-only/illustrative in the no-feed beta: NO live price (un-held tickers have
// no price source until the Pro feed). Mutations go through server actions; the SSR page
// fetch seeds the SWR cache (metron-ops#232) so first paint is instant, and a successful
// mutation revalidates just this key instead of a full `router.refresh()`. A failure
// surfaces inline (never silently swallowed).

import { useState, useTransition } from "react";
import type { WatchlistEntry } from "@/lib/api";
import { isoDate } from "@/lib/format";
import { Empty, Section, Table } from "@/components/ui";
import { addWatchlistAction, removeWatchlistAction } from "@/app/portfolios/[id]/actions";
import { useWatchlist } from "@/lib/use-watchlist";

export function WatchlistPanel({
  portfolioId,
  entries: fallbackEntries,
}: {
  portfolioId: string;
  entries: WatchlistEntry[];
}) {
  const { data: entries = fallbackEntries, mutate } = useWatchlist(portfolioId, fallbackEntries);
  const [symbol, setSymbol] = useState("");
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pending, start] = useTransition();

  function add(e: React.FormEvent) {
    e.preventDefault();
    const sym = symbol.trim().toUpperCase();
    if (!sym) return;
    setError(null);
    start(async () => {
      const r = await addWatchlistAction(portfolioId, sym, note);
      if (!r.ok) {
        setError(r.message);
        return;
      }
      setSymbol("");
      setNote("");
      void mutate();
    });
  }

  function remove(sym: string) {
    setError(null);
    start(async () => {
      const r = await removeWatchlistAction(portfolioId, sym);
      if (!r.ok) {
        setError(r.message);
        return;
      }
      void mutate();
    });
  }

  return (
    <div>
      <form onSubmit={add} className="mt-3 flex flex-wrap items-center gap-2">
        <input
          value={symbol}
          onChange={(e) => setSymbol(e.target.value)}
          placeholder="Ticker (e.g. NVDA)"
          aria-label="Ticker to add"
          className="w-40 rounded border border-line bg-surface px-2 py-1 text-sm uppercase tabular-nums"
        />
        <input
          value={note}
          onChange={(e) => setNote(e.target.value)}
          placeholder="Note (optional)"
          aria-label="Note"
          className="w-56 rounded border border-line bg-surface px-2 py-1 text-sm"
        />
        <button
          type="submit"
          disabled={pending || !symbol.trim()}
          className="rounded border border-line px-3 py-1 text-sm font-medium hover:bg-white/5 disabled:opacity-50"
        >
          Add
        </button>
      </form>
      {error ? <p className="mt-2 text-xs text-negative">{error}</p> : null}

      <Section
        title="Tracked tickers"
        note="reference data only in beta — live prices arrive with the Pro market-data feed"
      >
        {entries.length === 0 ? (
          <Empty>
            Your watchlist is empty — it doesn&apos;t auto-populate. Add a ticker in the box above (e.g. NVDA) to
            track a name you don&apos;t hold. In the beta you&apos;ll see its name &amp; sector; live prices and
            earnings dates arrive with the Pro market-data feed.
          </Empty>
        ) : (
          <Table head={["Ticker", "Name", "Sector", "Next earnings", "Status", "Note", ""]}>
            {entries.map((e) => (
              <tr key={e.symbol} className="border-b border-line last:border-0">
                <td className="px-4 py-2 font-medium tabular-nums">{e.symbol}</td>
                <td className="px-4 py-2 text-muted">{e.name ?? "—"}</td>
                <td className="px-4 py-2 text-muted">{e.sector ?? "—"}</td>
                <td className="px-4 py-2 tabular-nums text-muted">
                  {e.next_earnings_date ? isoDate(e.next_earnings_date) : "—"}
                </td>
                <td className="px-4 py-2">
                  {e.held ? (
                    <span className="rounded bg-positive/10 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-positive">
                      Held
                    </span>
                  ) : (
                    <span className="text-[10px] uppercase tracking-wide text-muted">Watching</span>
                  )}
                </td>
                <td className="px-4 py-2 text-muted">{e.note ?? "—"}</td>
                <td className="px-4 py-2 text-right">
                  <button
                    type="button"
                    onClick={() => remove(e.symbol)}
                    disabled={pending}
                    aria-label={`Remove ${e.symbol}`}
                    className="rounded px-2 py-0.5 text-xs text-muted hover:bg-rose-500/10 hover:text-rose-300 disabled:opacity-50"
                  >
                    Remove
                  </button>
                </td>
              </tr>
            ))}
          </Table>
        )}
      </Section>
    </div>
  );
}
