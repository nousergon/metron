"use client";

// Crypto wallet tracking (metron-ops#111) — add/remove BTC+ETH wallet addresses and see
// their synced balances + USD value. Standalone: decoupled from the EOD-close holdings/NAV
// (crypto is 24/7). Balances are synced by the nousergon-data producer; a row shows
// "Pending sync" until the first balance arrives. Mutations go through server actions that
// revalidate the page; failures surface inline (never silently swallowed).

import { useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import type { CryptoSummary } from "@/lib/api";
import { money, quantity } from "@/lib/format";
import { Empty, Section, Table } from "@/components/ui";
import { addCryptoAddressAction, deleteCryptoAddressAction } from "@/app/portfolios/[id]/actions";

const CHAINS = [
  { value: "BTC", label: "Bitcoin (BTC)" },
  { value: "ETH", label: "Ethereum (ETH)" },
];

function short(addr: string): string {
  return addr.length > 16 ? `${addr.slice(0, 8)}…${addr.slice(-6)}` : addr;
}

export function CryptoPanel({ portfolioId, summary }: { portfolioId: string; summary: CryptoSummary }) {
  const router = useRouter();
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
      router.refresh();
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
      router.refresh();
    });
  }

  const { positions, total_usd, n_pending, as_of_utc, stale } = summary;
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
          placeholder="Wallet address"
          aria-label="Wallet address"
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
            <Table head={["Chain", "Wallet", "Balance", "Price", "Value", ""]}>
              {positions.map((p) => (
                <tr key={p.id} className="border-b border-line last:border-0">
                  <td className="px-4 py-2 font-medium">{p.chain}</td>
                  <td className="px-4 py-2 text-muted">
                    <span className="font-mono" title={p.address}>
                      {short(p.address)}
                    </span>
                    {p.label ? <span className="ml-2 text-xs">{p.label}</span> : null}
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
                    <button
                      type="button"
                      onClick={() => remove(p.id)}
                      disabled={pending}
                      aria-label={`Remove ${p.chain} wallet`}
                      className="rounded px-2 py-0.5 text-xs text-muted hover:bg-rose-500/10 hover:text-rose-300 disabled:opacity-50"
                    >
                      Remove
                    </button>
                  </td>
                </tr>
              ))}
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
