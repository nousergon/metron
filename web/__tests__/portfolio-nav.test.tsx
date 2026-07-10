// PortfolioNav — the Pages dropdown: current-page resolution, the navQuery carrying
// the account selection onto selection-scoped links ONLY, and plugin page append.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

let pathname = "/portfolios/p";
vi.mock("next/navigation", () => ({ usePathname: () => pathname }));

import { PortfolioNav } from "@/components/portfolio-nav";

beforeEach(() => {
  pathname = "/portfolios/p";
});

function open() {
  fireEvent.click(screen.getByRole("button"));
}

describe("PortfolioNav", () => {
  it("shows the current page on the trigger (Holdings at the portfolio root, metron-ops-I156)", () => {
    render(<PortfolioNav portfolioId="p" navQuery="" />);
    expect(screen.getByRole("button")).toHaveTextContent("Holdings");
  });

  it("resolves the Overview at its own route", () => {
    pathname = "/portfolios/p/overview";
    render(<PortfolioNav portfolioId="p" navQuery="" />);
    expect(screen.getByRole("button")).toHaveTextContent("Overview");
  });

  it("resolves a sub-page as current", () => {
    pathname = "/portfolios/p/tax";
    render(<PortfolioNav portfolioId="p" navQuery="" />);
    expect(screen.getByRole("button")).toHaveTextContent("Tax");
  });

  it("carries navQuery onto selection-scoped links but NOT whole-portfolio pages", () => {
    render(<PortfolioNav portfolioId="p" navQuery="?account_id=a1" />);
    open();
    const href = (name: string) => screen.getByRole("menuitem", { name })!.getAttribute("href");
    expect(href("Performance")).toBe("/portfolios/p/performance?account_id=a1");
    // Holdings is the landing page (base route); Overview never scopes.
    expect(href("Holdings")).toBe("/portfolios/p?account_id=a1");
    expect(href("Overview")).toBe("/portfolios/p/overview");
    expect(href("Tax")).toBe("/portfolios/p/tax?account_id=a1");
    // Calendar/Settings are whole-portfolio surfaces — no selection query.
    expect(href("Calendar")).toBe("/portfolios/p/calendar");
    expect(href("Settings & data")).toBe("/portfolios/p/settings");
  });

  it("appends premium plugin pages to the menu", () => {
    render(
      <PortfolioNav
        portfolioId="p"
        navQuery=""
        plugins={[{ id: "advisor", label: "Advisor", href: "advisor" }]}
      />,
    );
    open();
    expect(screen.getByRole("menuitem", { name: "Advisor" })).toHaveAttribute("href", "/portfolios/p/advisor");
  });

  it("HIDES a feed-dependent page (Risk) when the feed entitlement is off (beta)", () => {
    render(
      <PortfolioNav
        portfolioId="p"
        navQuery=""
        featureStates={{
          overview: { available: true, required_tier: "beta" },
          risk: { available: false, required_tier: "personal" },
        }}
      />,
    );
    open();
    // metron-ops#53: feed-dependent pages are hidden in the no-feed beta, not shown locked.
    expect(screen.queryByRole("menuitem", { name: /Risk/ })).not.toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: "Overview" })).toHaveAttribute("href");
  });

  it("locks a NON-feed feature the active tier excludes — non-clickable, upsell tier", () => {
    render(
      <PortfolioNav
        portfolioId="p"
        navQuery=""
        featureStates={{ performance: { available: false, required_tier: "personal" } }}
      />,
    );
    open();
    const perf = screen.getByRole("menuitem", { name: /Performance/ });
    expect(perf).toHaveAttribute("aria-disabled", "true");
    expect(perf).not.toHaveAttribute("href"); // not a link
    expect(perf).toHaveTextContent("Intelligence"); // upsell badge
  });

  it("leaves all pages clickable when no featureStates given (ungated)", () => {
    render(<PortfolioNav portfolioId="p" navQuery="" />);
    open();
    expect(screen.getByRole("menuitem", { name: "Risk" })).toHaveAttribute("href");
  });

  it("closes on Escape", () => {
    render(<PortfolioNav portfolioId="p" navQuery="" />);
    open();
    expect(screen.getByRole("menu")).toBeInTheDocument();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });
});
