// AccountPanel — the selection + delete behaviors that would regress silently:
// the await-save-before-URL-clear ordering (clearing to All must persist BEFORE the
// URL empties, or the page redirects back into the stale saved filter), toggle
// normalization (all/none → empty URL), and the delete flow pruning the selection.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

// vi.mock factories are hoisted above imports — everything they touch must be too.
const mocks = vi.hoisted(() => ({
  replace: vi.fn(),
  refresh: vi.fn(),
  urlAccountIds: [] as string[],
  saveAccountSelectionAction: vi.fn(async (_pid: string, _ids: string[]) => ({ ok: true, message: "" })),
  deleteAccountAction: vi.fn(async (_pid: string, _aid: string) => ({ ok: true, message: "" })),
}));
const { replace, refresh, saveAccountSelectionAction, deleteAccountAction } = mocks;

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: mocks.replace, refresh: mocks.refresh }),
  usePathname: () => "/portfolios/p",
  useSearchParams: () => new URLSearchParams(mocks.urlAccountIds.map((id) => ["account_id", id])),
}));

vi.mock("@/app/portfolios/[id]/actions", () => ({
  saveAccountSelectionAction: mocks.saveAccountSelectionAction,
  deleteAccountAction: mocks.deleteAccountAction,
}));

import { AccountPanel } from "@/components/account-panel";
import type { Account } from "@/lib/api";

const acct = (id: string, name: string): Account =>
  ({
    account_id: id,
    broker: "snaptrade",
    external_id: id.toUpperCase(),
    name,
    currency: "USD",
    nickname: null,
    institution: "E-Trade",
    account_type: null,
    tax_treatment: null,
    taxable: true,
    cost_basis_base: 1000,
    market_value: 1200,
    unrealized_gain: 200,
    n_unconverted: 0,
    overnight_pct: null,
    intraday_pct: null,
    day_pct: 0.01,
    ytd_pct: 0.25,
    ltm_pct: 0.18,
  }) as Account;

const ACCOUNTS = [acct("a1", "Brokerage"), acct("a2", "IRA"), acct("a3", "Roth")];

function renderPanel(props?: { selectable?: boolean; deletable?: boolean }) {
  return render(<AccountPanel accounts={ACCOUNTS} baseCurrency="USD" portfolioId="p" {...props} />);
}

beforeEach(() => {
  replace.mockClear();
  refresh.mockClear();
  saveAccountSelectionAction.mockClear();
  deleteAccountAction.mockClear();
  mocks.urlAccountIds = [];
  vi.spyOn(window, "confirm").mockReturnValue(true);
});

describe("selection", () => {
  it("unchecking one account pushes the rest into the URL and persists them", async () => {
    renderPanel();
    fireEvent.click(screen.getByLabelText("Include Brokerage"));
    await waitFor(() => expect(replace).toHaveBeenCalled());
    const url = replace.mock.calls[0][0] as string;
    expect(url).toContain("account_id=a2");
    expect(url).toContain("account_id=a3");
    expect(url).not.toContain("account_id=a1");
    const savedIds = saveAccountSelectionAction.mock.calls[0]![1];
    expect(new Set(savedIds)).toEqual(new Set(["a2", "a3"]));
  });

  it("flips the checkbox OPTIMISTICALLY — instantly, before the URL/navigation resolves", () => {
    // The lag fix (metron-ops): the box must not wait for the ~0.5–1s server round-trip.
    // With router.replace mocked (the URL never actually changes), the box can only move
    // via the optimistic local state — so a synchronous flip proves the optimism.
    renderPanel(); // urlAccountIds [] → all three checked
    const box = screen.getByLabelText("Include Brokerage") as HTMLInputElement;
    expect(box.checked).toBe(true);
    fireEvent.click(box);
    expect(box.checked).toBe(false); // instant, no awaiting navigation
  });

  it("checking the last unchecked account normalizes back to the whole portfolio (empty URL)", async () => {
    mocks.urlAccountIds = ["a1", "a2"];
    renderPanel();
    fireEvent.click(screen.getByLabelText("Include Roth")); // now all 3 → normalize to []
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/portfolios/p", { scroll: false }));
    expect(saveAccountSelectionAction).toHaveBeenCalledWith("p", []);
  });

  it("clearing to All AWAITS the save before dropping URL params (stale-filter race)", async () => {
    mocks.urlAccountIds = ["a1"];
    let resolveSave!: (v: { ok: boolean; message: string }) => void;
    saveAccountSelectionAction.mockReturnValueOnce(
      new Promise((res) => {
        resolveSave = res;
      }),
    );
    renderPanel();
    fireEvent.click(screen.getByLabelText("All accounts"));
    // The URL must NOT change while the save is in flight — the page would
    // re-render with no params and redirect back into the stale saved filter.
    await Promise.resolve();
    expect(replace).not.toHaveBeenCalled();
    resolveSave({ ok: true, message: "" });
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/portfolios/p", { scroll: false }));
  });
});

describe("per-account period returns (metron-ops#87)", () => {
  it("renders Day / YTD / LTM columns with each account's returns", () => {
    renderPanel(); // 3 accounts, each ytd 25.0% / ltm 18.0% / day 1.0%
    expect(screen.getByText("YTD")).toBeInTheDocument();
    expect(screen.getByText("LTM")).toBeInTheDocument();
    expect(screen.getByText("Day")).toBeInTheDocument();
    expect(screen.getAllByText("25.0%").length).toBe(3); // YTD per account
    expect(screen.getAllByText("18.0%").length).toBe(3); // LTM per account
  });
});

describe("totals track the selection (match the headline Total value)", () => {
  it("viewing all sums every account and labels the row 'All accounts total'", () => {
    renderPanel(); // urlAccountIds [] → whole portfolio
    const totalRow = screen.getByText("All accounts total").parentElement!;
    expect(totalRow).toHaveTextContent("$3,600"); // 3 × $1,200
    expect(screen.queryByText("Selected accounts total")).not.toBeInTheDocument();
  });

  it("a scoped selection sums only the selected accounts and relabels the total", () => {
    mocks.urlAccountIds = ["a1", "a2"]; // a3 excluded
    renderPanel();
    const totalRow = screen.getByText("Selected accounts total").parentElement!;
    expect(totalRow).toHaveTextContent("$2,400"); // 2 × $1,200, NOT all three
    expect(screen.queryByText("All accounts total")).not.toBeInTheDocument();
  });
});

describe("cash balance is folded into the displayed total (metron-ops)", () => {
  // Root-cause regression: a connector-computed cash balance was silently dropped
  // before persistence, undercounting every such account's displayed total (live
  // case: $20.3k missing from the Crucible reference-rate sleeve). market_value and
  // cash are separate API fields; this panel recombines them for display.
  const withCash = (id: string, name: string, cash: number | null): Account =>
    ({ ...acct(id, name), cash }) as Account;

  it("a row's Balance column includes both market value and cash", () => {
    render(
      <AccountPanel
        accounts={[withCash("a1", "Brokerage", 20_300)]}
        baseCurrency="USD"
        portfolioId="p"
      />,
    );
    // market_value 1200 (from the acct() fixture) + cash 20,300 = 21,500. Appears
    // twice — once on the row, once on the (single-account) grand total.
    expect(screen.getAllByText("$21,500").length).toBe(2);
  });

  it("the grand total sums cash across every account", () => {
    render(
      <AccountPanel
        accounts={[withCash("a1", "Brokerage", 1_000), withCash("a2", "IRA", 500)]}
        baseCurrency="USD"
        portfolioId="p"
      />,
    );
    const totalRow = screen.getByText("All accounts total").parentElement!;
    // (1200 + 1000) + (1200 + 500) = 3,900.
    expect(totalRow).toHaveTextContent("$3,900");
  });

  it("a known cash-only account isn't hidden by a null market_value", () => {
    const cashOnly = { ...withCash("a1", "Sweep", 5_000), market_value: null } as Account;
    render(<AccountPanel accounts={[cashOnly]} baseCurrency="USD" portfolioId="p" />);
    const totalRow = screen.getByText("All accounts total").parentElement!;
    expect(totalRow).toHaveTextContent("$5,000");
  });

  it("an account with neither market_value nor cash shows '—', not $0", () => {
    const unknown = { ...withCash("a1", "Unsynced", null), market_value: null } as Account;
    render(<AccountPanel accounts={[unknown]} baseCurrency="USD" portfolioId="p" />);
    const totalRow = screen.getByText("All accounts total").parentElement!;
    expect(totalRow).toHaveTextContent("—");
  });
});

describe("delete (deletable mode — the Overview, metron-ops#77)", () => {
  it("deletes after confirm, prunes the id from the URL selection, refreshes", async () => {
    mocks.urlAccountIds = ["a1", "a2"];
    renderPanel({ deletable: true });
    fireEvent.click(screen.getByLabelText("Delete Brokerage"));
    await waitFor(() => expect(deleteAccountAction).toHaveBeenCalledWith("p", "a1"));
    await waitFor(() => expect(replace).toHaveBeenCalled());
    const url = replace.mock.calls.at(-1)![0] as string;
    expect(url).not.toContain("account_id=a1");
    expect(url).toContain("account_id=a2");
    expect(refresh).toHaveBeenCalled();
  });

  it("declined confirm does nothing", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(false);
    renderPanel({ deletable: true });
    fireEvent.click(screen.getByLabelText("Delete Brokerage"));
    expect(deleteAccountAction).not.toHaveBeenCalled();
  });

  it("a failed delete surfaces the error and leaves the selection alone", async () => {
    deleteAccountAction.mockResolvedValueOnce({ ok: false, message: "Delete failed — backend reachable?" });
    renderPanel({ deletable: true });
    fireEvent.click(screen.getByLabelText("Delete Brokerage"));
    await waitFor(() => expect(screen.getByText("Delete failed — backend reachable?")).toBeInTheDocument());
    expect(replace).not.toHaveBeenCalled();
    expect(refresh).not.toHaveBeenCalled();
  });
});

describe("mode split — selectable vs deletable (metron-ops#77)", () => {
  it("the Holdings filter view (selectable, default) has checkboxes + All toggle, no delete/management", () => {
    renderPanel(); // defaults: selectable, not deletable
    expect(screen.getByLabelText("Include Brokerage")).toBeInTheDocument();
    expect(screen.getByLabelText("All accounts")).toBeInTheDocument();
    expect(screen.queryByLabelText("Delete Brokerage")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Tax treatment for Brokerage")).not.toBeInTheDocument();
  });

  it("the Overview management view (deletable, not selectable) has delete, no tax dropdown, no checkboxes", () => {
    renderPanel({ selectable: false, deletable: true });
    expect(screen.getByLabelText("Delete Brokerage")).toBeInTheDocument();
    // Tax-treatment editing moved to the Settings page — no inline dropdown here.
    expect(screen.queryByLabelText("Tax treatment for Brokerage")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Include Brokerage")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("All accounts")).not.toBeInTheDocument();
  });
});

describe("tax-status grouping", () => {
  const taxed = (id: string, name: string, treatment: string | null): Account =>
    ({ ...acct(id, name), tax_treatment: treatment, taxable: treatment !== "tax_deferred" && treatment !== "tax_exempt" }) as Account;

  it("groups accounts by tax status with subtotals + a grand total", () => {
    const mixed = [
      taxed("a1", "Brokerage", "taxable"),
      taxed("a2", "IRA", "tax_deferred"),
      taxed("a3", "HSA", "tax_exempt"),
    ];
    render(<AccountPanel accounts={mixed} baseCurrency="USD" portfolioId="p" />);
    // One group header per distinct tax status + their subtotals, plus a grand total.
    expect(screen.getByText("Taxable · 1")).toBeInTheDocument();
    expect(screen.getByText("Tax-deferred · 1")).toBeInTheDocument();
    expect(screen.getByText("Tax-exempt · 1")).toBeInTheDocument();
    expect(screen.getByText("Taxable subtotal")).toBeInTheDocument();
    expect(screen.getByText("All accounts total")).toBeInTheDocument();
    // All three rows still carry their selection checkboxes (grouping is presentational).
    expect(screen.getByLabelText("Include Brokerage")).toBeInTheDocument();
    expect(screen.getByLabelText("Include IRA")).toBeInTheDocument();
  });

  it("a single tax status shows the grand total but no per-group subtotals", () => {
    renderPanel(); // ACCOUNTS are all taxable → one group
    expect(screen.getByText("All accounts total")).toBeInTheDocument();
    expect(screen.queryByText("Taxable subtotal")).not.toBeInTheDocument();
    expect(screen.queryByText(/Taxable · /)).not.toBeInTheDocument();
  });

  it("shows each account's tax status as a read-only label (editing lives on Settings)", () => {
    const mixed = [taxed("a1", "Brokerage", "taxable"), taxed("a2", "IRA", "tax_deferred")];
    render(<AccountPanel accounts={mixed} baseCurrency="USD" portfolioId="p" deletable />);
    // No inline dropdown on any row — the tax treatment is editable only on the Settings page.
    expect(screen.queryByLabelText("Tax treatment for Brokerage")).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
    // The grouping still surfaces the category as a read-only label.
    expect(screen.getByText("Taxable · 1")).toBeInTheDocument();
    expect(screen.getByText("Tax-deferred · 1")).toBeInTheDocument();
  });
});
