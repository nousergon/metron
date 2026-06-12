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
  }) as Account;

const ACCOUNTS = [acct("a1", "Brokerage"), acct("a2", "IRA"), acct("a3", "Roth")];

function renderPanel() {
  return render(<AccountPanel accounts={ACCOUNTS} baseCurrency="USD" portfolioId="p" />);
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

describe("delete", () => {
  it("deletes after confirm, prunes the id from the URL selection, refreshes", async () => {
    mocks.urlAccountIds = ["a1", "a2"];
    renderPanel();
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
    renderPanel();
    fireEvent.click(screen.getByLabelText("Delete Brokerage"));
    expect(deleteAccountAction).not.toHaveBeenCalled();
  });

  it("a failed delete surfaces the error and leaves the selection alone", async () => {
    deleteAccountAction.mockResolvedValueOnce({ ok: false, message: "Delete failed — backend reachable?" });
    renderPanel();
    fireEvent.click(screen.getByLabelText("Delete Brokerage"));
    await waitFor(() => expect(screen.getByText("Delete failed — backend reachable?")).toBeInTheDocument());
    expect(replace).not.toHaveBeenCalled();
    expect(refresh).not.toHaveBeenCalled();
  });
});
