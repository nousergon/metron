// resolveAccountIds — the saved-selection resolution every portfolio page runs.
// URL selection must always win; with none, a saved selection redirects into the
// equivalent URL; a prefs fetch failure must never break the page (whole portfolio).

import { beforeEach, describe, expect, it, vi } from "vitest";

// vi.mock factories are hoisted above imports — the mock fns must be hoisted too.
const { redirect, getAccountSelection } = vi.hoisted(() => ({
  redirect: vi.fn((url: string) => {
    // Next's redirect() throws — model that so code after it never runs.
    throw Object.assign(new Error("NEXT_REDIRECT"), { url });
  }),
  getAccountSelection: vi.fn(),
}));

vi.mock("next/navigation", () => ({ redirect }));
vi.mock("@/lib/api", () => ({
  getAccountSelection,
  // resolveAccountIds builds the redirect query with the real helper's contract:
  acctParams: (ids: string[]) =>
    ids.length === 0 ? "" : "?" + ids.map((a) => `account_id=${encodeURIComponent(a)}`).join("&"),
}));

import { resolveAccountIds } from "@/lib/selection";

beforeEach(() => {
  redirect.mockClear();
  getAccountSelection.mockReset();
});

describe("resolveAccountIds", () => {
  it("URL selection wins — saved selection is not even fetched", async () => {
    const ids = await resolveAccountIds("t", "p", "/portfolios/p", ["a1", "a2"]);
    expect(ids).toEqual(["a1", "a2"]);
    expect(getAccountSelection).not.toHaveBeenCalled();
    expect(redirect).not.toHaveBeenCalled();
  });

  it("a single URL id (non-array searchParam shape) normalizes to a list", async () => {
    const ids = await resolveAccountIds("t", "p", "/portfolios/p", "a1");
    expect(ids).toEqual(["a1"]);
  });

  it("no URL selection + saved selection → redirects into the equivalent URL", async () => {
    getAccountSelection.mockResolvedValue(["a1", "a2"]);
    await expect(resolveAccountIds("t", "p", "/portfolios/p/tax", undefined)).rejects.toThrow("NEXT_REDIRECT");
    expect(redirect).toHaveBeenCalledWith("/portfolios/p/tax?account_id=a1&account_id=a2");
  });

  it("no URL selection + empty saved selection → whole portfolio, no redirect", async () => {
    getAccountSelection.mockResolvedValue([]);
    const ids = await resolveAccountIds("t", "p", "/portfolios/p", undefined);
    expect(ids).toEqual([]);
    expect(redirect).not.toHaveBeenCalled();
  });

  it("prefs fetch failure is best-effort → whole portfolio, never a broken page", async () => {
    getAccountSelection.mockRejectedValue(new Error("backend down"));
    const ids = await resolveAccountIds("t", "p", "/portfolios/p", undefined);
    expect(ids).toEqual([]);
    expect(redirect).not.toHaveBeenCalled();
  });
});
