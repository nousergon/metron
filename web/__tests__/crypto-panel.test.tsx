// Crypto panel (P-23, data-collection-plan §7 R2): the crypto-balances producer (D38) has
// been paused since 2026-08-07 (Ruled, Brian 2026-09-14: (c) — stay paused, render as-of +
// STALE, never serve the file as current). This pins the never-hide / never-error contract.

import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn() }) }));
vi.mock("@/app/portfolios/[id]/actions", () => ({
  addCryptoAddressAction: vi.fn(),
  deleteCryptoAddressAction: vi.fn(),
  fetchCryptoAction: vi.fn(),
}));

import { CryptoPanel } from "@/components/crypto-panel";
import { fetchCryptoAction } from "@/app/portfolios/[id]/actions";
import type { CryptoSummary } from "@/lib/api";

const summary = (over: Partial<CryptoSummary> = {}): CryptoSummary => ({
  available: true,
  as_of_utc: "2026-06-29T12:00:00Z",
  stale: false,
  total_usd: 1000,
  n_pending: 0,
  positions: [
    {
      id: "11111111-1111-1111-1111-111111111111",
      chain: "BTC",
      address: "bc1qxyz",
      label: null,
      symbol: "BTC",
      balance: 0.02,
      price_usd: 50000,
      value_usd: 1000,
      synced: true,
    },
  ],
  reason: null,
  ...over,
});

describe("CryptoPanel — stale rendering (P-23)", () => {
  it("shows as-of without a STALE badge when fresh", () => {
    const s = summary();
    vi.mocked(fetchCryptoAction).mockResolvedValue(s);
    render(<CryptoPanel portfolioId={crypto.randomUUID()} summary={s} />);
    expect(screen.getByText(/as of/)).toBeInTheDocument();
    expect(screen.queryByText("STALE")).not.toBeInTheDocument();
  });

  it("renders 'as of <date> · STALE' and never hides the panel when the producer artifact is stale", () => {
    const s = summary({
      available: false,
      stale: true,
      total_usd: null,
      reason: "stale",
      positions: [
        {
          id: "11111111-1111-1111-1111-111111111111",
          chain: "BTC",
          address: "bc1qxyz",
          label: null,
          symbol: null,
          balance: null,
          price_usd: null,
          value_usd: null,
          synced: false,
        },
      ],
    });
    vi.mocked(fetchCryptoAction).mockResolvedValue(s);
    render(<CryptoPanel portfolioId={crypto.randomUUID()} summary={s} />);
    // Never hidden — the wallet list + section still render.
    expect(screen.getByText("Crypto wallets")).toBeInTheDocument();
    expect(screen.getByText(/as of/)).toBeInTheDocument();
    expect(screen.getByText("STALE")).toBeInTheDocument();
    // Never rendered as an error — no error text anywhere in the panel.
    expect(screen.queryByText(/error/i)).not.toBeInTheDocument();
  });

  it("falls back to the empty-state note when nothing has ever synced (no as_of)", () => {
    const s = summary({ available: false, as_of_utc: null, stale: true, total_usd: null, reason: "unavailable" });
    vi.mocked(fetchCryptoAction).mockResolvedValue(s);
    render(<CryptoPanel portfolioId={crypto.randomUUID()} summary={s} />);
    expect(screen.getByText("balances sync automatically once a wallet is added")).toBeInTheDocument();
    expect(screen.queryByText("STALE")).not.toBeInTheDocument();
  });
});
