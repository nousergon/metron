// Saved Holdings-table view (metron-ops#114): HoldingsView hydrates the grouping from the
// saved value and persists every control change fire-and-forget. The COLUMN PRESET is
// session-only since 2026-07-08 — landing always opens on the regime-appropriate default
// (Intraday while live, Overview for settled — page.tsx, 2026-07-22), never a saved band set.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

const push = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, refresh: vi.fn() }),
  usePathname: () => "/portfolios/p1/holdings",
  useSearchParams: () => new URLSearchParams(""),
}));
const saveHoldingsViewAction = vi.fn();
vi.mock("@/app/portfolios/[id]/actions", () => ({
  saveHoldingsViewAction: (...args: unknown[]) => saveHoldingsViewAction(...args),
  setSecurityLabelAction: vi.fn(),
  setSecurityClassificationAction: vi.fn(),
}));

import { HoldingsView } from "@/components/holdings-view";
import type { Holding } from "@/lib/api";

const h = (ticker: string): Holding =>
  ({
    ticker,
    quantity: 10,
    avg_cost: 100,
    cost_basis: 1000,
    currency: "USD",
    fx_rate: 1,
    last_price: 120,
    last_price_date: "2024-06-03",
    market_value_local: 1200,
    cost_basis_base: 1000,
    market_value: 1200,
    unrealized_gain: 200,
    unrealized_pct: 0.2,
    security_type: "equity",
    account_id: null,
    account_label: null,
    sector: "Technology",
    country: "United States",
    attractiveness: 70,
    pe: 30.2,
  }) as Holding;

describe("HoldingsView saved-view hydration", () => {
  it("hydrates the grouping from the saved value but ALWAYS lands on the Overview preset", () => {
    render(
      <HoldingsView
        holdings={[h("AAPL")]}
        baseCurrency="USD"
        priced
        medians={null}
        portfolioId="p1"
        savedGrouping="classification"
      />,
    );
    // Saved grouping is active...
    expect(screen.getByRole("button", { name: "By sector → country" })).toHaveAttribute("aria-pressed", "true");
    // ...but the column set is the lean Overview default — no analytic band leaks in
    // from a prior session (P/E 30.2× is a Valuation-only cell).
    expect(screen.getByRole("button", { name: "Overview" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.queryByText("30.2×")).not.toBeInTheDocument();
  });

  it("defaults to asset-class + the lean Overview preset when nothing is saved", () => {
    render(<HoldingsView holdings={[h("AAPL")]} baseCurrency="USD" priced medians={null} portfolioId="p1" />);
    expect(screen.getByRole("button", { name: "By asset class" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.queryByText("30.2×")).not.toBeInTheDocument(); // Overview = Position + Value, no Valuation cells
  });

  it("anchors the column-band control beneath the Portfolio total bar (metron-ops#118+)", () => {
    render(<HoldingsView holdings={[h("AAPL")]} baseCurrency="USD" priced medians={null} portfolioId="p1" />);
    // The control ("Columns") renders with the total bar, not stranded in the top toolbar.
    expect(screen.getByText("Portfolio total")).toBeInTheDocument();
    expect(screen.getByText("Columns")).toBeInTheDocument();
  });

  it("persists the full view when a control changes — with visible_bands always null", () => {
    render(<HoldingsView holdings={[h("AAPL")]} baseCurrency="USD" priced medians={null} portfolioId="p1" />);
    saveHoldingsViewAction.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "By sector → country" }));
    expect(saveHoldingsViewAction).toHaveBeenCalledWith("p1", {
      grouping: "classification",
      // Session-only preset: never persisted; null also clears pre-2026-07-08 saved sets.
      visible_bands: null,
      combine_by_account: false,
      hidden_types: [],
      valuation: "settled",
    });
  });

  it("does NOT persist a column-preset switch (session-only by design)", () => {
    render(<HoldingsView holdings={[h("AAPL")]} baseCurrency="USD" priced medians={null} portfolioId="p1" />);
    saveHoldingsViewAction.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "Valuation" }));
    // The preset switches in-session (Valuation band renders)...
    expect(screen.getByText("30.2×")).toBeInTheDocument();
    // ...but nothing is written to the saved view.
    expect(saveHoldingsViewAction).not.toHaveBeenCalled();
  });

  it("offers the valuation toggle only when live is available, and persists + routes the switch", () => {
    // Not offered without liveAvailable (no feed / intraday toggle off) — the settled
    // regime is then the only regime (metron-ops#153).
    const { unmount } = render(
      <HoldingsView holdings={[h("AAPL")]} baseCurrency="USD" priced medians={null} portfolioId="p1" />,
    );
    expect(screen.queryByRole("button", { name: "Live session" })).not.toBeInTheDocument();
    unmount();

    render(
      <HoldingsView
        holdings={[h("AAPL")]}
        baseCurrency="USD"
        priced
        medians={null}
        portfolioId="p1"
        valuation="live"
        liveAvailable
        sessionState="live"
      />,
    );
    expect(screen.getByRole("button", { name: "Live session" })).toHaveAttribute("aria-pressed", "true");
    saveHoldingsViewAction.mockClear();
    push.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "Settled close" }));
    // Persists the full view including the regime, and re-fetches via the URL (?val=).
    expect(saveHoldingsViewAction).toHaveBeenCalledWith("p1", expect.objectContaining({ valuation: "settled" }));
    expect(push).toHaveBeenCalledWith("/portfolios/p1/holdings?val=settled");
  });

  it("labels the session option by market state, honestly — never disabled (metron-ops-I156, superseded 2026-07-22)", () => {
    // recap: post-close same day — same data, honestly framed, still clickable.
    const { unmount } = render(
      <HoldingsView holdings={[h("AAPL")]} baseCurrency="USD" priced medians={null} portfolioId="p1" valuation="live" liveAvailable sessionState="recap" />,
    );
    expect(screen.getByRole("button", { name: "Today's session" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Today's session" })).not.toBeDisabled();
    unmount();

    // closed: pre-market / weekend — frozen at the last session, labeled "Last session",
    // still clickable and can be the active regime (the freeze-not-hide behavior).
    const { unmount: unmount2 } = render(
      <HoldingsView holdings={[h("AAPL")]} baseCurrency="USD" priced medians={null} portfolioId="p1" valuation="live" liveAvailable sessionState="closed" />,
    );
    const liveBtn = screen.getByRole("button", { name: "Last session" });
    expect(liveBtn).not.toBeDisabled();
    expect(liveBtn).toHaveAttribute("aria-pressed", "true");
    unmount2();

    // Settled close remains a normal, always-available explicit opt-out.
    render(
      <HoldingsView holdings={[h("AAPL")]} baseCurrency="USD" priced medians={null} portfolioId="p1" valuation="settled" liveAvailable sessionState="closed" />,
    );
    const liveBtn2 = screen.getByRole("button", { name: "Last session" });
    expect(liveBtn2).not.toBeDisabled();
    fireEvent.click(liveBtn2);
    expect(push).toHaveBeenCalledWith(expect.stringContaining("val=live"));
    expect(screen.getByRole("button", { name: "Settled close" })).toHaveAttribute("aria-pressed", "true");
  });
});
