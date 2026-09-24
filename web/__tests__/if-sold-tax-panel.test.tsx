// IfSoldTaxPanel (metron-ops#208) — the "if sold" tax estimate over a hypothetical the
// user describes. Locks: nothing is pre-selected (empty ticker, empty lot quantities, no
// gain/loss ordering); the server's ST/LT split, estimate and per-lot rows render verbatim;
// placeholder rates are called out as not the user's; specific lots send the picks and
// show the delta vs FIFO; the copy is facts-only (source scan below).

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

const mocks = vi.hoisted(() => ({
  fetchIfSoldAction: vi.fn(),
}));

vi.mock("@/app/portfolios/[id]/tax-whatif-action", () => ({
  fetchIfSoldAction: mocks.fetchIfSoldAction,
}));

import { IfSoldTaxPanel } from "@/components/if-sold-tax-panel";
import { ifSoldPath, type IfSoldPreview, type IfSoldSale } from "@/lib/api-tax-whatif";

const OPEN_LOTS = [
  { lot_index: 0, open_date: "2023-01-10", quantity: 10, cost_per_share: 100, cost_basis: 1000, holding_days: 873, term: "Long-term" },
  { lot_index: 1, open_date: "2024-03-01", quantity: 5, cost_per_share: 150, cost_basis: 750, holding_days: 457, term: "Long-term" },
  { lot_index: 2, open_date: "2025-02-01", quantity: 10, cost_per_share: 200, cost_basis: 2000, holding_days: 120, term: "Short-term" },
];

const FIFO: IfSoldSale = {
  method: "fifo",
  quantity: 25,
  price: 180,
  proceeds: 4500,
  cost_basis: 3750,
  gain_st: -200,
  gain_lt: 950,
  gain_total: 750,
  taxable_st: 0,
  taxable_lt: 750,
  est_tax_st: 0,
  est_tax_lt: 112.5,
  est_tax_state: 0,
  est_tax_total: 112.5,
  lots: OPEN_LOTS.map((l) => ({ ...l, proceeds: l.quantity * 180, gain: l.quantity * 180 - l.cost_basis })),
};

const PREVIEW: IfSoldPreview = {
  as_of: "2025-06-01",
  ticker: "AAPL",
  currency: "USD",
  price: 180,
  price_source: "latest_close",
  price_as_of: "2025-05-30",
  quantity_held: 25,
  rates: { short_term: 0.24, long_term: 0.15, state: 0 },
  rates_placeholder: ["short_term", "long_term", "state"],
  open_lots: OPEN_LOTS,
  sale: FIFO,
  fifo: FIFO,
  delta_vs_fifo: null,
  n_accounts_excluded: 1,
  history_incomplete: false,
};

const SPECIFIC: IfSoldSale = {
  ...FIFO,
  method: "specific",
  quantity: 10,
  proceeds: 1800,
  cost_basis: 2000,
  gain_st: -200,
  gain_lt: 0,
  gain_total: -200,
  taxable_lt: 0,
  est_tax_lt: 0,
  est_tax_total: 0,
  lots: [{ ...OPEN_LOTS[2], proceeds: 1800, gain: -200 }],
};

function estimate() {
  fireEvent.click(screen.getByRole("button", { name: /^estimate$/i }));
}

describe("IfSoldTaxPanel", () => {
  it("pre-selects nothing: empty holding, full-position and latest-close defaults", () => {
    render(<IfSoldTaxPanel portfolioId="p1" tickers={["MSFT", "AAPL"]} />);
    expect((screen.getByLabelText("Holding to estimate") as HTMLInputElement).value).toBe("");
    expect(screen.getByLabelText("Hypothetical sale quantity").getAttribute("placeholder")).toBe("Full position");
    expect(screen.getByLabelText("Hypothetical sale price").getAttribute("placeholder")).toBe("Latest close");
    expect(screen.getByRole("button", { name: /^estimate$/i })).toHaveProperty("disabled", true);
    // Specific lots needs the holding's lots first.
    expect(screen.getByRole("button", { name: /specific lots/i })).toHaveProperty("disabled", true);
    // Typeahead options are alphabetical — no ordering by gain, loss, or size.
    const options = Array.from(document.querySelectorAll("datalist option")).map((o) => o.getAttribute("value"));
    expect(options).toEqual(["AAPL", "MSFT"]);
  });

  it("renders the ST/LT split, estimated tax, per-lot rows and flags placeholder rates", async () => {
    mocks.fetchIfSoldAction.mockResolvedValue({ ok: true, preview: PREVIEW });
    render(<IfSoldTaxPanel portfolioId="p1" tickers={["AAPL"]} accountIds={["a1"]} />);
    fireEvent.change(screen.getByLabelText("Holding to estimate"), { target: { value: "AAPL" } });
    estimate();
    await waitFor(() => expect(screen.getByText(/were sold at/)).toBeTruthy());

    expect(mocks.fetchIfSoldAction).toHaveBeenLastCalledWith("p1", expect.objectContaining({
      ticker: "AAPL", quantity: undefined, price: undefined, method: "fifo", lots: undefined,
      st_rate: undefined, lt_rate: undefined, state_rate: undefined, accountIds: ["a1"],
    }));
    expect(screen.getByText("Short-term gain/loss")).toBeTruthy();
    expect(screen.getByText("Long-term gain/loss")).toBeTruthy();
    expect(screen.getByText("$112.50")).toBeTruthy();
    const rows = within(screen.getByRole("table", { name: "Lots in the hypothetical sale" })).getAllByRole("row");
    expect(rows).toHaveLength(1 + 3);
    const note = screen.getByTestId("placeholder-rates");
    expect(note.textContent).toMatch(/Placeholder rates, not your rates/);
    expect(note.textContent).toMatch(/federal short-term 24%/);
    expect(screen.getByText(/1 tax-advantaged account/)).toBeTruthy();
  });

  it("sends the user's rates as fractions and states them without the placeholder note", async () => {
    mocks.fetchIfSoldAction.mockResolvedValue({
      ok: true,
      preview: { ...PREVIEW, rates: { short_term: 0.32, long_term: 0.2, state: 0.05 }, rates_placeholder: [] },
    });
    render(<IfSoldTaxPanel portfolioId="p1" tickers={["AAPL"]} />);
    fireEvent.change(screen.getByLabelText("Holding to estimate"), { target: { value: "AAPL" } });
    fireEvent.change(screen.getByLabelText("Hypothetical sale quantity"), { target: { value: "12" } });
    fireEvent.change(screen.getByLabelText("Hypothetical sale price"), { target: { value: "190" } });
    fireEvent.change(screen.getByLabelText("Federal short-term rate"), { target: { value: "32" } });
    fireEvent.change(screen.getByLabelText("Federal long-term rate"), { target: { value: "20" } });
    fireEvent.change(screen.getByLabelText("State (flat) rate"), { target: { value: "5" } });
    estimate();
    await waitFor(() => expect(screen.getByText(/At the rates you entered/)).toBeTruthy());
    const input = mocks.fetchIfSoldAction.mock.lastCall?.[1];
    expect(input).toMatchObject({ quantity: 12, price: 190, st_rate: 0.32, lt_rate: 0.2 });
    expect(input.state_rate).toBeCloseTo(0.05);
    expect(screen.queryByTestId("placeholder-rates")).toBeNull();
  });

  it("specific lots: empty picks by default, sends the picks, shows the delta vs FIFO", async () => {
    mocks.fetchIfSoldAction.mockResolvedValueOnce({ ok: true, preview: PREVIEW });
    render(<IfSoldTaxPanel portfolioId="p1" tickers={["AAPL"]} />);
    fireEvent.change(screen.getByLabelText("Holding to estimate"), { target: { value: "AAPL" } });
    estimate();
    await waitFor(() => expect(screen.getByRole("button", { name: /specific lots/i })).toHaveProperty("disabled", false));

    fireEvent.click(screen.getByRole("button", { name: /specific lots/i }));
    const picker = screen.getByRole("table", { name: "Open lots to include" });
    const inputs = within(picker).getAllByRole("spinbutton") as HTMLInputElement[];
    expect(inputs.map((i) => i.value)).toEqual(["", "", ""]); // nothing pre-selected

    // Submitting with no picks is refused client-side.
    estimate();
    expect(screen.getByRole("alert").textContent).toMatch(/at least one lot/);

    mocks.fetchIfSoldAction.mockResolvedValueOnce({
      ok: true,
      preview: {
        ...PREVIEW,
        sale: SPECIFIC,
        delta_vs_fifo: { cost_basis: 800, gain_st: 0, gain_lt: -950, gain_total: -950, est_tax_total: -112.5 },
      },
    });
    fireEvent.change(screen.getByLabelText("Quantity from lot opened 2025-02-01"), { target: { value: "10" } });
    expect((screen.getByLabelText("Hypothetical sale quantity") as HTMLInputElement).value).toBe("10");
    estimate();
    await waitFor(() => expect(screen.getByTestId("delta-vs-fifo")).toBeTruthy());
    expect(mocks.fetchIfSoldAction.mock.lastCall?.[1]).toMatchObject({
      method: "specific", quantity: 10, lots: [{ lot_index: 2, quantity: 10 }],
    });
    expect(screen.getByTestId("delta-vs-fifo").textContent).toMatch(/Selected lots vs FIFO/);
    expect(screen.getByTestId("delta-vs-fifo").textContent).toMatch(/FIFO estimate \$112\.50/);
  });

  it("changing the holding resets to FIFO and clears picks", async () => {
    mocks.fetchIfSoldAction.mockResolvedValue({ ok: true, preview: PREVIEW });
    render(<IfSoldTaxPanel portfolioId="p1" tickers={["AAPL"]} />);
    fireEvent.change(screen.getByLabelText("Holding to estimate"), { target: { value: "AAPL" } });
    estimate();
    await waitFor(() => expect(screen.getByRole("button", { name: /specific lots/i })).toHaveProperty("disabled", false));
    fireEvent.click(screen.getByRole("button", { name: /specific lots/i }));
    fireEvent.change(screen.getByLabelText("Holding to estimate"), { target: { value: "MSFT" } });
    expect(screen.queryByRole("table", { name: "Open lots to include" })).toBeNull();
    expect(screen.getByRole("button", { name: /fifo/i }).getAttribute("aria-pressed")).toBe("true");
  });

  it("validates inputs client-side and surfaces server errors", async () => {
    render(<IfSoldTaxPanel portfolioId="p1" tickers={[]} />);
    fireEvent.change(screen.getByLabelText("Holding to estimate"), { target: { value: "AAPL" } });
    fireEvent.change(screen.getByLabelText("Hypothetical sale quantity"), { target: { value: "0" } });
    estimate();
    expect(screen.getByRole("alert").textContent).toMatch(/greater than zero/);

    fireEvent.change(screen.getByLabelText("Hypothetical sale quantity"), { target: { value: "-3" } });
    estimate();
    expect(screen.getByRole("alert").textContent).toMatch(/positive numbers/);

    fireEvent.change(screen.getByLabelText("Hypothetical sale quantity"), { target: { value: "" } });
    fireEvent.change(screen.getByLabelText("Federal long-term rate"), { target: { value: "150" } });
    estimate();
    expect(screen.getByRole("alert").textContent).toMatch(/between 0 and 100/);

    fireEvent.change(screen.getByLabelText("Federal long-term rate"), { target: { value: "" } });
    mocks.fetchIfSoldAction.mockResolvedValue({ ok: false, message: "No cached close for AAPL — enter a price for the hypothetical." });
    estimate();
    await waitFor(() => expect(screen.getByRole("alert").textContent).toMatch(/enter a price/));
    expect(mocks.fetchIfSoldAction).toHaveBeenCalled();
  });

  it("notes an incomplete lot history and a price the user entered", async () => {
    mocks.fetchIfSoldAction.mockResolvedValue({
      ok: true,
      preview: { ...PREVIEW, history_incomplete: true, price_source: "user", price_as_of: null, n_accounts_excluded: 0 },
    });
    render(<IfSoldTaxPanel portfolioId="p1" tickers={["AAPL"]} />);
    fireEvent.change(screen.getByLabelText("Holding to estimate"), { target: { value: "AAPL" } });
    estimate();
    await waitFor(() => expect(screen.getByText(/starts mid-position/)).toBeTruthy());
    expect(screen.getByText(/price you entered/)).toBeTruthy();
    expect(screen.queryByText(/tax-advantaged account/)).toBeNull();
  });

  it("collapsible variant stays closed until opened", () => {
    render(<IfSoldTaxPanel portfolioId="p1" tickers={["AAPL"]} collapsible />);
    expect(screen.queryByLabelText("Holding to estimate")).toBeNull();
    fireEvent.click(screen.getByText(/If sold — tax estimate/));
    expect(screen.getByLabelText("Holding to estimate")).toBeTruthy();
  });

  it("renders without error at a 390px viewport", () => {
    const original = window.innerWidth;
    Object.defineProperty(window, "innerWidth", { writable: true, configurable: true, value: 390 });
    try {
      render(<IfSoldTaxPanel portfolioId="p1" tickers={["AAPL"]} />);
      expect(screen.getByRole("button", { name: /^estimate$/i })).toBeTruthy();
    } finally {
      Object.defineProperty(window, "innerWidth", { writable: true, configurable: true, value: original });
    }
  });
});

describe("ifSoldPath", () => {
  it("encodes every input, with repeatable lots and accounts", () => {
    const path = ifSoldPath("p 1", {
      ticker: " aapl ",
      quantity: 12,
      price: 190.5,
      method: "specific",
      lots: [{ lot_index: 2, quantity: 10 }, { lot_index: 1, quantity: 2 }],
      st_rate: 0.32,
      lt_rate: 0.2,
      state_rate: 0.05,
      accountIds: ["a1", "a2"],
    });
    expect(path).toBe(
      "/portfolios/p%201/tax/if-sold?ticker=aapl&quantity=12&price=190.5&method=specific&lot=2%3A10&lot=1%3A2" +
        "&st_rate=0.32&lt_rate=0.2&state_rate=0.05&account_id=a1&account_id=a2",
    );
    expect(ifSoldPath("p1", { ticker: "AAPL" })).toBe("/portfolios/p1/tax/if-sold?ticker=AAPL");
  });
});

// Descriptive-only copy lint (metron-ops#208 closes-when): the surface shows the tax math
// of a user-specified hypothetical and never suggests, ranks, or recommends a sale. Source
// scan, not DOM scan, so an untriggered branch (an error string, an empty state) is covered.
describe("if-sold surface copy lint", () => {
  const FILES = [
    "components/if-sold-tax-panel.tsx",
    "lib/api-tax-whatif.ts",
    "app/portfolios/[id]/tax-whatif-action.ts",
  ];
  const BANNED = [
    /recommend/i,
    /\bshould\b/i,
    /suggest/i,
    /\badvi[cs]e/i,
    /harvest/i,
    /optimal|optimi[sz]e/i,
    /\bbest\b/i,
    /you could save/i,
    /consider selling/i,
  ];
  for (const file of FILES) {
    it(`${file} carries no directive vocabulary`, () => {
      const text = readFileSync(resolve(__dirname, "..", file), "utf8");
      for (const pattern of BANNED) {
        expect(pattern.test(text), `${file} matches ${pattern}`).toBe(false);
      }
    });
  }
});
