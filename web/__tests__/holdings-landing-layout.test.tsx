// Holdings landing-page layout (Brian, 2026-09-28, reviewed on a phone): the Portfolio total
// leads the page content, the what-if / if-sold panels live on their own page, and the
// explanatory notes sit at the very bottom. The page is an async Server Component whose
// sections stream behind <Suspense>, so its ORDER is asserted on the returned element tree
// (without rendering the async children); the client pieces are rendered normally.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { isValidElement, type ReactElement, type ReactNode } from "react";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), refresh: vi.fn() }),
  usePathname: () => "/portfolios/p1",
  useSearchParams: () => new URLSearchParams(""),
  redirect: vi.fn(),
}));
vi.mock("@/app/portfolios/[id]/actions", () => ({
  saveHoldingsViewAction: vi.fn(),
  setSecurityLabelAction: vi.fn(),
  setSecurityClassificationAction: vi.fn(),
}));
vi.mock("@/app/portfolios/[id]/tax-whatif-action", () => ({ fetchIfSoldAction: vi.fn() }));
vi.mock("@/lib/session", () => ({ requireApiAuth: async () => "jwt" }));
vi.mock("@/lib/entitlements", () => ({
  navFeatureStates: async () => ({}),
  loadEntitlements: async () => null,
}));
vi.mock("@/lib/selection", () => ({ resolveAccountIds: async () => [] }));

const api = vi.hoisted(() => ({
  getSummary: vi.fn(),
  getHoldings: vi.fn(),
  getHoldingsView: vi.fn(),
  getIntradayStatus: vi.fn(),
  getToday: vi.fn(),
  getIntradayLegs: vi.fn(),
}));
vi.mock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), ...api }));

import { HoldingsView } from "@/components/holdings-view";
import { FilteredPortfolioTotal, HiddenTypesProvider } from "@/components/holdings-filter-context";
import type { Holding, IntradayStatus } from "@/lib/api";

const h = (ticker: string, security_type = "equity", mv = 1200): Holding =>
  ({
    ticker,
    quantity: 10,
    avg_cost: 100,
    cost_basis: 1000,
    currency: "USD",
    fx_rate: 1,
    last_price: mv / 10,
    last_price_date: "2026-09-28",
    market_value_local: mv,
    cost_basis_base: 1000,
    market_value: mv,
    unrealized_gain: mv - 1000,
    unrealized_pct: (mv - 1000) / 1000,
    security_type,
    account_id: null,
    account_label: null,
    sector: "Technology",
    country: "United States",
  }) as Holding;

const liveStatus: IntradayStatus = {
  applied: true,
  as_of_utc: "2026-09-28T15:20:00Z",
  stale: false,
  n_priced: 1,
  n_total: 1,
  n_estimated: 0,
  reason: null,
  covered_nav: 1200,
  total_nav: 1200,
  sources: {},
  session_state: "live",
};

/** Depth-first element type names (function components by name, host tags by tag). */
function typeNames(node: ReactNode, out: string[] = []): string[] {
  if (Array.isArray(node)) {
    for (const n of node) typeNames(n, out);
    return out;
  }
  if (!isValidElement(node)) return out;
  const el = node as ReactElement<{ children?: ReactNode; id?: string }>;
  const t = el.type as unknown;
  const name =
    typeof t === "string" ? (el.props.id ? `${t}#${el.props.id}` : t) : ((t as { name?: string }).name ?? "?");
  out.push(name);
  typeNames(el.props.children, out);
  return out;
}

beforeEach(() => {
  vi.clearAllMocks();
  api.getSummary.mockResolvedValue({ base_currency: "USD", market_value: 1200 });
  api.getHoldings.mockResolvedValue([h("AAPL")]);
  api.getHoldingsView.mockResolvedValue(null);
  api.getIntradayStatus.mockResolvedValue(liveStatus);
  api.getToday.mockResolvedValue(null);
  api.getIntradayLegs.mockResolvedValue(null);
});

describe("Holdings landing page composition", () => {
  async function landing() {
    const mod = await import("@/app/portfolios/[id]/page");
    return mod.default({ params: Promise.resolve({ id: "p1" }), searchParams: Promise.resolve({}) });
  }

  it("orders total → live session → holdings/watchlist → notes, with no what-if panels", async () => {
    const names = typeNames(await landing());
    const at = (n: string) => {
      const i = names.indexOf(n);
      expect(i, `${n} missing from ${names.join(",")}`).toBeGreaterThanOrEqual(0);
      return i;
    };
    expect(at("TotalSection")).toBeLessThan(at("SessionSection"));
    expect(at("SessionSection")).toBeLessThan(at("HoldingsSection"));
    expect(at("HoldingsSection")).toBeLessThan(at("WatchlistSection"));
    expect(at("WatchlistSection")).toBeLessThan(at("h2#holdings-notes"));
    expect(at("h2#holdings-notes")).toBeLessThan(at("SessionNotesSection"));
    expect(at("h2#holdings-notes")).toBeLessThan(at("FreshnessNotesSection"));
    expect(names).not.toContain("HoldingsWhatIfPanel");
    expect(names).not.toContain("IfSoldTaxPanel");
  });

  it("reads holdings ONCE for the total, the table and the notes", async () => {
    await landing();
    expect(api.getHoldings).toHaveBeenCalledTimes(1);
  });
});

describe("HoldingsView on the landing page", () => {
  it("no longer renders the what-if or if-sold panels", () => {
    render(<HoldingsView holdings={[h("AAPL")]} baseCurrency="USD" priced medians={null} portfolioId="p1" />);
    expect(screen.queryByText("What-if: try hypothetical weights")).not.toBeInTheDocument();
    expect(screen.queryByText(/If sold — tax estimate/)).not.toBeInTheDocument();
  });

  it("hoisted mode drops the inline total but keeps the column control", () => {
    render(
      <HoldingsView holdings={[h("AAPL")]} baseCurrency="USD" priced medians={null} portfolioId="p1" hoistTotalAndNotes />,
    );
    expect(screen.queryByText("Portfolio total")).not.toBeInTheDocument();
    expect(screen.getByText("Columns")).toBeInTheDocument();
  });

  it("the hoisted total sums the same type-filtered rows as the table", () => {
    const holdings = [h("AAPL", "equity", 1200), h("VMFXX", "cash", 5000)];
    render(
      <HiddenTypesProvider initialHidden={["cash"]}>
        <FilteredPortfolioTotal holdings={holdings} baseCurrency="USD" priced />
      </HiddenTypesProvider>,
    );
    expect(screen.getByText("Portfolio total")).toBeInTheDocument();
    expect(screen.getByText("$1,200")).toBeInTheDocument(); // cash hidden → AAPL only
    expect(screen.queryByText("$6,200")).not.toBeInTheDocument();
  });
});

describe("What-if page", () => {
  async function whatIf() {
    const mod = await import("@/app/portfolios/[id]/what-if/page");
    return mod.default({ params: Promise.resolve({ id: "p1" }), searchParams: Promise.resolve({}) });
  }

  it("hosts both hypothetical panels, the weights sandbox open", async () => {
    api.getHoldings.mockResolvedValue([h("AAPL"), h("MSFT")]);
    render(await whatIf());
    expect(screen.getByRole("heading", { level: 1, name: "What-if" })).toBeInTheDocument();
    expect(screen.getByText("What-if: try hypothetical weights")).toBeInTheDocument();
    expect(screen.getByText("Current weight")).toBeInTheDocument(); // expanded by default
    expect(screen.getByLabelText("Holding to estimate")).toBeInTheDocument(); // if-sold panel, inline
    // Settled holdings, whole portfolio (no saved-selection redirect).
    expect(api.getHoldings).toHaveBeenCalledWith("jwt", "p1", []);
  });

  it("shows the empty state with no positions", async () => {
    api.getHoldings.mockResolvedValue([]);
    render(await whatIf());
    expect(screen.getByText("No open positions.")).toBeInTheDocument();
    expect(screen.queryByText("What-if: try hypothetical weights")).not.toBeInTheDocument();
  });
});
