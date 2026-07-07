// Accounts scope chip (metron-ops-I156) — account selection as a toolbar popover,
// driving the same ?account_id= machinery the old panel used (lib/use-account-selection).

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

const replace = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace, refresh: vi.fn() }),
  usePathname: () => "/portfolios/p1",
  useSearchParams: () => new URLSearchParams(""),
}));
const saveAccountSelectionAction = vi.fn();
vi.mock("@/app/portfolios/[id]/actions", () => ({
  saveAccountSelectionAction: (...args: unknown[]) => saveAccountSelectionAction(...args),
}));

import { AccountScopeChip } from "@/components/account-scope-chip";
import type { Account } from "@/lib/api";

const acct = (id: string, name: string, mv: number, taxable = true): Account =>
  ({
    account_id: id,
    external_id: id,
    name,
    nickname: null,
    tax_treatment: taxable ? "taxable" : "tax_deferred",
    taxable,
    market_value: mv,
  }) as Account;

const ACCOUNTS = [acct("a1", "IBKR Brokerage", 500_000), acct("a2", "401k", 200_000, false)];

beforeEach(() => {
  replace.mockClear();
  saveAccountSelectionAction.mockClear();
});

describe("AccountScopeChip", () => {
  it("renders the all-accounts state and opens a grouped popover with market values", () => {
    render(<AccountScopeChip accounts={ACCOUNTS} baseCurrency="USD" portfolioId="p1" />);
    const trigger = screen.getByRole("button", { name: /Accounts \(all\)/ });
    fireEvent.click(trigger);
    expect(screen.getByText("IBKR Brokerage")).toBeInTheDocument();
    expect(screen.getByText("Taxable")).toBeInTheDocument();      // tax-group subhead
    expect(screen.getByText("Tax-deferred")).toBeInTheDocument();
    expect(screen.getByText("$500,000")).toBeInTheDocument();     // per-account MV
  });

  it("unchecking an account routes the scoped selection and persists it", () => {
    render(<AccountScopeChip accounts={ACCOUNTS} baseCurrency="USD" portfolioId="p1" />);
    fireEvent.click(screen.getByRole("button", { name: /Accounts/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: /401k/ }));
    expect(saveAccountSelectionAction).toHaveBeenCalledWith("p1", ["a1"]);
    expect(replace).toHaveBeenCalledWith("/portfolios/p1?account_id=a1", { scroll: false });
  });

  it("renders nothing for a single-account portfolio (nothing to scope)", () => {
    const { container } = render(
      <AccountScopeChip accounts={[ACCOUNTS[0]]} baseCurrency="USD" portfolioId="p1" />,
    );
    expect(container).toBeEmptyDOMElement();
  });
});
