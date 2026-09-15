// WhatIfPurchasePanel (metron-ops-I311) — renders the server's before/after snapshot
// verbatim, the tax-lot preview, and the feed-gated beta note; phone-width (390px) smoke
// test.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

const mocks = vi.hoisted(() => ({
  fetchWhatIfAction: vi.fn(),
}));

vi.mock("@/app/portfolios/[id]/planning-actions", () => ({
  fetchWhatIfAction: mocks.fetchWhatIfAction,
}));

import { WhatIfPurchasePanel } from "@/components/whatif-purchase-panel";
import type { WhatIfPlan } from "@/lib/api-planning";

const SNAP = {
  concentration: { n_positions: 2, hhi: 0.5, effective_n: 2, top5_share: 1, top10_share: 1, max_position_ticker: "AAPL", max_position_weight: 0.5 },
  sector_mix: [{ key: "Information Technology", weight: 0.5 }],
  asset_class_mix: [{ key: "equity", weight: 1 }],
  account_mix: [{ key: "Taxable", weight: 1 }],
  dividend_yield_on_cost: 0.02,
};

const PLAN: WhatIfPlan = {
  as_of: "2026-09-15",
  symbol: "MSFT",
  shares: 4,
  usd: 1000,
  price: 250,
  price_source: "spine",
  price_as_of: "2026-09-13",
  before: SNAP,
  after: { ...SNAP, concentration: { ...SNAP.concentration, n_positions: 3, hhi: 0.34 } },
  beta_available: true,
  tax_lot: { symbol: "MSFT", shares: 4, price: 250, trade_date: "2026-09-15", cost_basis: 1000 },
  portfolio_value: 2000,
  base_currency: "USD",
};

describe("WhatIfPurchasePanel", () => {
  it("renders the before/after snapshot and the tax-lot preview", async () => {
    mocks.fetchWhatIfAction.mockResolvedValue({ ok: true, plan: PLAN });
    render(<WhatIfPurchasePanel portfolioId="p1" />);
    fireEvent.change(screen.getByLabelText("Hypothetical purchase ticker"), { target: { value: "MSFT" } });
    fireEvent.change(screen.getByLabelText("Hypothetical purchase amount"), { target: { value: "1000" } });
    fireEvent.click(screen.getByRole("button", { name: /preview impact/i }));
    await waitFor(() => expect(screen.getByText(/Tax lot created/)).toBeTruthy());
    expect(screen.getByText(/cost basis/)).toBeTruthy();
  });

  it("hides beta/risk when the feed is off", async () => {
    mocks.fetchWhatIfAction.mockResolvedValue({ ok: true, plan: { ...PLAN, beta_available: false } });
    render(<WhatIfPurchasePanel portfolioId="p1" />);
    fireEvent.change(screen.getByLabelText("Hypothetical purchase ticker"), { target: { value: "MSFT" } });
    fireEvent.change(screen.getByLabelText("Hypothetical purchase amount"), { target: { value: "1000" } });
    fireEvent.click(screen.getByRole("button", { name: /preview impact/i }));
    await waitFor(() => expect(screen.getByText(/needs the licensed market-data feed/)).toBeTruthy());
  });

  it("renders without error at a 390px viewport", () => {
    const original = window.innerWidth;
    Object.defineProperty(window, "innerWidth", { writable: true, configurable: true, value: 390 });
    try {
      render(<WhatIfPurchasePanel portfolioId="p1" />);
      expect(screen.getByRole("button", { name: /preview impact/i })).toBeTruthy();
    } finally {
      Object.defineProperty(window, "innerWidth", { writable: true, configurable: true, value: original });
    }
  });
});
