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
  it("shows the current page on the trigger (Overview at the portfolio root)", () => {
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
    expect(href("Tax")).toBe("/portfolios/p/tax?account_id=a1");
    // Macro/Calendar/Settings are whole-portfolio surfaces — no selection query.
    expect(href("Macro")).toBe("/portfolios/p/macro");
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

  it("locks a feature the active tier excludes — non-clickable, with the upsell tier", () => {
    render(
      <PortfolioNav
        portfolioId="p"
        navQuery=""
        featureStates={{
          overview: { available: true, required_tier: "beta" },
          risk: { available: false, required_tier: "pro" },
        }}
      />,
    );
    open();
    const risk = screen.getByRole("menuitem", { name: /Risk/ });
    expect(risk).toHaveAttribute("aria-disabled", "true");
    expect(risk).not.toHaveAttribute("href"); // not a link
    expect(risk).toHaveTextContent("Pro"); // upsell badge
    // Available + ungated pages stay clickable links.
    expect(screen.getByRole("menuitem", { name: "Overview" })).toHaveAttribute("href");
    expect(screen.getByRole("menuitem", { name: "Calendar" })).toHaveAttribute("href");
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
