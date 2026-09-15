// External user demo (metron-ops-I310): the web tier's page allowlist, credential
// mapping, and the nav's external mode (only usable pages plus the locked cards).
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

vi.mock("next/navigation", () => ({ usePathname: () => "/portfolios/p/glance" }));

import { authHeaders, cacheIdentity } from "@/lib/api";
import {
  DEMO_HOUSEHOLD_PORTFOLIO_ID,
  EXTERNAL_DEMO_CREDENTIAL_PREFIX,
  isExternalDemoPageAllowed,
} from "@/lib/external-demo";
import { EXTERNAL_DEMO_NAV_KEY, PortfolioNav, type NavFeatureState } from "@/components/portfolio-nav";

const H = `/portfolios/${DEMO_HOUSEHOLD_PORTFOLIO_ID}`;

describe("isExternalDemoPageAllowed", () => {
  it("allows the household's no-advice pages", () => {
    for (const p of [H, `${H}/glance`, `${H}/risk`, `${H}/plan`, `${H}/tearsheet/DEMO-AAPL`, `${H}/in-development`]) {
      expect(isExternalDemoPageAllowed(p)).toBe(true);
    }
  });

  it("denies advice pages, settings, other portfolios and the dashboard", () => {
    for (const p of [
      `${H}/market`,
      `${H}/research-intel`,
      `${H}/advisor`,
      `${H}/alpha-engine`,
      `${H}/intelligence`,
      `${H}/settings`,
      `${H}/crypto`,
      "/portfolios/00000000-0000-0000-0000-00000000de62/glance",
      "/",
    ]) {
      expect(isExternalDemoPageAllowed(p)).toBe(false);
    }
  });
});

describe("external-demo credential", () => {
  const cred = `${EXTERNAL_DEMO_CREDENTIAL_PREFIX}tok`;

  it("is sent as X-Demo-Session, never as a bearer token", () => {
    expect(authHeaders(cred)).toEqual({ "X-Demo-Session": "tok" });
  });

  it("keys caches on one shared identity", () => {
    expect(cacheIdentity(cred)).toBe("external-demo");
  });
});

describe("PortfolioNav external mode", () => {
  const states: Record<string, NavFeatureState> = {
    glance: { available: true, required_tier: null },
    overview: { available: true, required_tier: null },
    risk: { available: true, required_tier: null },
    research_intel: { available: false, required_tier: "personal" },
    market_board: { available: false, required_tier: "personal" },
    cash_to_targets: { available: true, required_tier: null },
    [EXTERNAL_DEMO_NAV_KEY]: { available: true, required_tier: null },
  };

  it("shows only usable pages plus In development", () => {
    render(
      <PortfolioNav
        portfolioId="p"
        navQuery=""
        featureStates={states}
        plugins={[{ id: "x", label: "Plugin page", href: "x" }]}
      />,
    );
    fireEvent.click(screen.getByRole("button"));
    const labels = screen.getAllByRole("menuitem").map((el) => el.textContent);
    expect(labels).toContain("Glance");
    expect(labels).toContain("Risk");
    expect(labels).toContain("Watchlist");
    expect(labels).toContain("In development");
    for (const hidden of ["Research intel", "Market", "Settings & data", "Crypto", "Plugin page"]) {
      expect(labels).not.toContain(hidden);
    }
  });
});
