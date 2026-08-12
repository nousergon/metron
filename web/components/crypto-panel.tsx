"use client";

// Crypto wallet tracking (metron-ops#111) — add/remove BTC+ETH wallet addresses and see
// their synced balances + USD value. Standalone: decoupled from the EOD-close holdings/NAV
// (crypto is 24/7). Balances are synced by the nousergon-data producer; a row shows
// "Pending sync" until the first balance arrives. Mutations call `mutate()` on the SWR key
// (metron-ops#232) so only the crypto data re-fetches instead of a full page refresh.

import { useState, useTransition } from "react";
import type { CryptoSummary } from "@/lib/api";
import { money, quantity } from "@/lib/format";
import { Empty, Section, Table } from "@/components/ui";
import { addCryptoAddressAction, deleteCryptoAddressAction } from "@/app/portfolios/[id]/actions";
import { useCrypto } from "@/lib/use-crypto";

const CHAINS = [
  { value: "BTC", label: "Bitcoin (BTC)" },
  { value: "ETH", label: "Ethereum (ETH)" },
];

function short(addr: string): string {
  return addr.length > 16 ? `${addr.slice(0, 8)}…${addr.slice(-6)}` : addr;
}

export function CryptoPanel({ portfolioId, summary }: { portfolioId: string; summary: CryptoSummary }) {
  const { data, mutate } = useCrypto(portfolioId, summary);
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
      void mutate();
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
      void mutate();
    });
  }

  // `data` is always defined because `fallbackData` seeds it from the server fetch
  const { positions, total_usd, n_pending, as_of_utc, stale } = data!;
  const asOfLocal = as_of_utc ? new Date(as_of_utc).toLocaleString() : null;

  return (
    <div>
      <form onSubmit={add} className="mt-3 flex flex-wrap items-center gap-2">
        <select
          value={chain}
          onChange={(e) => setChain(e.target.value)}
          aria-label="Chain"
          className="rounded border border-line bg-surface px-2 py-1 text-sm"
        >
          {CHAINS.map((c) => (
            <option key={c.value} value={c.value}>
              {c.label}
            </option>
          ))}
        </select>
        <input
          value={address}
          onChange={(e) => setAddress(e.target.value)}
          placeholder="Address — or BTC xpub/ypub/zpub for a whole wallet"
          aria-label="Wallet address or extended public key"
          className="w-96 max-w-full rounded border border-line bg-surface px-2 py-1 text-sm font-mono"
        />
        <input
          value={label}
          onChange={(e) => setLabel(e.target.value)}
          placeholder="Label (optional)"
          aria-label="Label"
          className="w-44 rounded border border-line bg-surface px-2 py-1 text-sm"
        />
        <button
          type="submit"
          disabled={pending || !address.trim()}
          className="rounded border border-line px-3 py-1 text-sm font-medium hover:bg-white/5 disabled:opacity-50"
        >
          Add wallet
        </button>
      </form>
      {error ? <p className="mt-2 text-xs text-negative">{error}</p> : null}

      <Section
        title="Crypto wallets"
        note={
          total_usd != null
            ? `total ${money(total_usd)}${stale ? " · sync delayed" : ""}${asOfLocal ? ` · as of ${asOfLocal}` : ""}`
            : "balances sync automatically once a wallet is added"
        }
      >
        {positions.length === 0 ? (
          <Empty>
            No wallets tracked yet. Add a BTC or ETH address above — balances sync automatically (this page is
            read-only; we never hold your keys).
          </Empty>
        ) : (
          <>
            <Table head={["Asset", "Wallet", "Balance", "Price", "Value", ""]}>
              {positions.map((p, i) => {
                // One wallet → many rows (native + ERC-20 tokens). Show the wallet address +
                // Remove only on the first row of each wallet; token rows indent under it.
                const firstOfWallet = i === 0 || positions[i - 1].id !== p.id;
                return (
                  <tr key={`${p.id}-${p.symbol ?? p.chain}`} className="border-b border-line last:border-0">
                    <td className={`px-4 py-2 ${firstOfWallet ? "font-medium" : "pl-8 text-muted"}`}>
                      {p.symbol ?? p.chain}
                    </td>
                    <td className="px-4 py-2 text-muted">
                      {firstOfWallet ? (
                        <>
                          <span className="font-mono" title={p.address}>
                            {short(p.address)}
                          </span>
                          {p.label ? <span className="ml-2 text-xs">{p.label}</span> : null}
                        </>
                      ) : null}
                    </td>
                    <td className="px-4 py-2 tabular-nums">{p.balance != null ? quantity(p.balance) : "—"}</td>
                    <td className="px-4 py-2 tabular-nums text-muted">{p.price_usd != null ? money(p.price_usd) : "—"}</td>
                    <td className="px-4 py-2 tabular-nums">
                      {p.synced && p.value_usd != null ? (
                        money(p.value_usd)
                      ) : (
                        <span className="text-[10px] uppercase tracking-wide text-muted">Pending sync</span>
                      )}
                    </td>
                    <td className="px-4 py-2 text-right">
                      {firstOfWallet ? (
                        <button
                          type="button"
                          onClick={() => remove(p.id)}
                          disabled={pending}
                          aria-label={`Remove ${p.chain} wallet`}
                          className="rounded px-2 py-0.5 text-xs text-muted hover:bg-rose-500/10 hover:text-rose-300 disabled:opacity-50"
                        >
                          Remove
                        </button>
                      ) : null}
                    </td>
                  </tr>
                );
              })}
            </Table>
            {n_pending > 0 ? (
              <p className="mt-2 text-xs text-muted">
                {n_pending} wallet{n_pending === 1 ? "" : "s"} awaiting first sync — balances appear within a few
                minutes of being added.
              </p>
            ) : null}
          </>
        )}
      </Section>
    </div>
  );
}
