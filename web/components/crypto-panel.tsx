"use client";

// Crypto wallet tracking (metron-ops#111) — add/remove BTC+ETH wallet addresses and see
// their synced balances + USD value. Standalone: decoupled from the EOD-close holdings/NAV
// (crypto is 24/7). Balances are synced by the nousergon-data producer; a row shows
// "Pending sync" until the first balance arrives. Mutations go through server actions that
// revalidate the SWR cache (metron-ops#232); failures surface inline (never silently swallowed).

import { useState, useTransition } from "react";
import { useSWRConfig } from "swr";
import type { CryptoSummary } from "@/lib/api";
import { money, quantity } from "@/lib/format";
import { Empty, Section, Table } from "@/components/ui";
import { addCryptoAddressAction, deleteCryptoAddressAction } from "@/app/portfolios/[id]/actions";
import { cryptoKey, useCrypto } from "@/lib/use-crypto";

const CHAINS = [
  { value: "BTC", label: "Bitcoin (BTC)" },
  { value: "ETH", label: "Ethereum (ETH)" },
];

function short(addr: string): string {
  return addr.length > 16 ? `${addr.slice(0, 8)}…${addr.slice(-6)}` : addr;
}

export function CryptoPanel({ portfolioId, summary: fallbackSummary }: { portfolioId: string; summary: CryptoSummary }) {
  const { mutate } = useSWRConfig();
  const { data: summary = fallbackSummary } = useCrypto(portfolioId, fallbackSummary);
  const [chain, setChain] = useState("BTC");
  const [address, setAddress] = useState("");
  const [label, setLabel] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pending, start] = useTransition();

  function add(e: React.FormEvent) {
    e.preventDefault();
    const addr = address.trim();
    if (!addr) return;
    setError(null);
    start(async () => {
      const r = await addCryptoAddressAction(portfolioId, chain, addr, label);
      if (!r.ok) {
        setError(r.message);
        return;
      }
      setAddress("");
      setLabel("");
      void mutate(cryptoKey(portfolioId));
    });
  }

  function remove(addressId: string) {
    setError(null);
    start(async () => {
      const r = await deleteCryptoAddressAction(portfolioId, addressId);
      if (!r.ok) {
        setError(r.message);
        return;
      }
      void mutate(cryptoKey(portfolioId));
    });
  }

  const { positions, total_usd, n_pending, as_of_utc, stale } = summary;
  const asOfLocal = as_of_utc ? new Date(as_of_utc).toLocaleString() : null;

  return (
    <>
      {positions.length > 0 && (
        <Section
          title="Wallet addresses"
          note={stale ? "Pending sync — balances shown may be stale" : asOfLocal ? `As of ${asOfLocal}` : undefined}
        >
          <Table head={["Chain", "Address", "Label", "Balance", "Price (USD)", "Value (USD)", ""]}>
              {positions.map((p) => (
                <tr key={p.id} className={p.synced ? "" : "text-muted"}>
                  <td className="font-mono text-xs">{p.chain}</td>
                  <td className="font-mono text-xs" title={p.address}>
                    {short(p.address)}
                  </td>
                  <td className="text-sm">{p.label ?? "—"}</td>
                  <td className="text-right font-mono text-xs">{p.balance != null ? quantity(p.balance) : "Pending"}</td>
                  <td className="text-right font-mono text-xs">
                    {p.price_usd != null ? money(p.price_usd) : "—"}
                  </td>
                  <td className="text-right font-mono text-xs">
                    {p.value_usd != null ? money(p.value_usd) : "—"}
                  </td>
                  <td>
                    <button
                      type="button"
                      onClick={() => remove(p.id)}
                      aria-label={`Remove ${short(p.address)}`}
                      title="Remove"
                      disabled={pending}
                      className="text-[11px] text-negative/70 transition hover:text-negative"
                    >
                      Remove
                    </button>
                  </td>
                </tr>
              ))}
              {n_pending > 0 && (
                <tr className="text-muted">
                  <td colSpan={7} className="py-2 text-center text-xs italic">
                    {n_pending} address{n_pending > 1 ? "es" : ""} awaiting first sync
                  </td>
                </tr>
              )}
          </Table>
        </Section>
      )}

      <Section title="Add address">
        <form onSubmit={add} className="flex flex-wrap items-end gap-2">
          <label className="flex flex-col gap-1 text-xs text-muted">
            Chain
            <select
              value={chain}
              onChange={(e) => setChain(e.target.value)}
              className="rounded border border-line bg-surface px-2 py-1 font-mono text-sm text-ink"
            >
              {CHAINS.map((c) => (
                <option key={c.value} value={c.value}>
                  {c.label}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-xs text-muted">
            Address
            <input
              value={address}
              onChange={(e) => setAddress(e.target.value)}
              placeholder="1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"
              disabled={pending}
              className="w-72 rounded border border-line bg-surface px-2 py-1 font-mono text-sm text-ink disabled:opacity-50"
            />
          </label>
          <label className="flex flex-col gap-1 text-xs text-muted">
            Label
            <input
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              placeholder="Optional label"
              disabled={pending}
              className="w-36 rounded border border-line bg-surface px-2 py-1 font-mono text-sm text-ink disabled:opacity-50"
            />
          </label>
          <button
            type="submit"
            disabled={pending || !address.trim()}
            className="rounded bg-ink px-3 py-[7px] text-sm text-surface transition hover:opacity-80 disabled:opacity-30"
          >
            {pending ? "Adding..." : "Add"}
          </button>
        </form>
        {error ? <div className="mt-1 text-xs text-negative">{error}</div> : null}
      </Section>
    </>
  );
}
