// Column resize (metron-ops#161): a real mousedown -> mousemove -> mouseup drag sequence on
// a column's resize handle must actually change that column's rendered width (the <colgroup>
// <col> TanStack's columnSizing state drives) — not just "the resize handle renders".

import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn() }) }));
vi.mock("@/app/portfolios/[id]/actions", () => ({
  setSecurityLabelAction: vi.fn(),
  setSecurityClassificationAction: vi.fn(),
}));

import { HoldingsTable } from "@/components/holdings-table";
import type { Holding } from "@/lib/api";

const h = (ticker: string, over: Partial<Holding> = {}): Holding =>
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
    sector: "Technology",
    country: "United States",
    pe: 30.2,
    ...over,
  }) as Holding;

describe("HoldingsTable column resize", () => {
  it("dragging the Ticker resize handle grows its <col> width", () => {
    render(
      <HoldingsTable baseCurrency="USD" priced holdings={[h("AAPL")]} visibleBands={["Valuation"]} />,
    );

    const cols = document.querySelectorAll("colgroup col");
    const tickerCol = cols[0] as HTMLElement;
    const before = tickerCol.style.width;

    const handle = screen.getByTestId("resize-ticker");
    fireEvent.mouseDown(handle, { clientX: 100 });
    fireEvent.mouseMove(document, { clientX: 180 });
    fireEvent.mouseUp(document);

    expect(tickerCol.style.width).not.toBe(before);
    // Grew by roughly the drag distance (TanStack's onChange resize mode applies deltas live).
    expect(parseInt(tickerCol.style.width, 10)).toBeGreaterThan(parseInt(before, 10));
  });

  it("dragging a band column resize handle changes only that column's width", () => {
    render(
      <HoldingsTable baseCurrency="USD" priced holdings={[h("AAPL")]} visibleBands={["Valuation"]} />,
    );

    const cols = Array.from(document.querySelectorAll("colgroup col")) as HTMLElement[];

    const peHandle = screen.getByTestId("resize-pe");
    const peHeaderCell = peHandle.closest("th")!;
    const peColOrdinal = Array.from(peHeaderCell.parentElement!.children).indexOf(peHeaderCell);
    const peCol = cols[peColOrdinal];
    const before = peCol.style.width;

    fireEvent.mouseDown(peHandle, { clientX: 50 });
    fireEvent.mouseMove(document, { clientX: 10 }); // drag left -> shrink
    fireEvent.mouseUp(document);

    expect(peCol.style.width).not.toBe(before);
    expect(parseInt(peCol.style.width, 10)).toBeLessThan(parseInt(before, 10));
  });
});
