// Public /methodology page (metron-ops-I205). Three properties:
//   1. it renders, with one anchored section per documented measurement;
//   2. every "How this is measured" link on an analytics page lands on an anchor that exists;
//   3. the copy stays descriptive — no directive, advice, or predictive vocabulary, checked
//      both in the rendered text and in the page source (so a banned word in a branch the
//      render never reaches is caught too, the same reasoning as plan-copy-lint).

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import MethodologyPage from "@/app/methodology/page";
import { MethodologyLink } from "@/components/methodology-link";

const ANCHORS = [
  "valuation",
  "performance",
  "risk-measures",
  "attribution",
  "factor-risk",
  "diagnostics",
  "income",
  "tax-lots",
  "technical-rating",
];

// Analytics page → the section its header link must point at.
const LINKED_PAGES: Record<string, string> = {
  "app/portfolios/[id]/performance/page.tsx": "performance",
  "app/portfolios/[id]/attribution/page.tsx": "attribution",
  "app/portfolios/[id]/risk/page.tsx": "factor-risk",
  "app/portfolios/[id]/tax/page.tsx": "tax-lots",
  "app/portfolios/[id]/diagnostics/page.tsx": "diagnostics",
};

const BANNED = [
  /\bshould\b/i,
  /\bought\b/i,
  /recommend/i,
  /\badvi[cs]e/i,
  /\bbuy\b/i,
  /\bsell\b/i,
  /\bpredict/i,
  /\bforecast/i,
  /\bguarantee/i,
  /\boutperform/i,
  /\bopportunit/i,
  /\bconsider\b/i,
];

describe("/methodology page", () => {
  it("renders the page heading", () => {
    render(<MethodologyPage />);
    expect(screen.getByRole("heading", { level: 1, name: "Methodology" })).toBeInTheDocument();
  });

  it("has an anchored section with a heading for every documented measurement", () => {
    const { container } = render(<MethodologyPage />);
    for (const id of ANCHORS) {
      const section = container.querySelector(`section#${id}`);
      expect(section, `section#${id}`).not.toBeNull();
      expect(section!.querySelector("h2")?.textContent?.trim()).toBeTruthy();
    }
    expect(container.querySelectorAll("section[id]")).toHaveLength(ANCHORS.length);
  });

  it("lists every section in the contents", () => {
    render(<MethodologyPage />);
    const nav = screen.getByRole("navigation", { name: "Contents" });
    const hrefs = Array.from(nav.querySelectorAll("a")).map((a) => a.getAttribute("href"));
    expect(hrefs).toEqual(ANCHORS.map((id) => `#${id}`));
  });

  it("describes the factor model as the ETF regression it is", () => {
    const { container } = render(<MethodologyPage />);
    const text = container.querySelector("section#factor-risk")!.textContent ?? "";
    for (const etf of ["SPY", "MTUM", "QUAL", "USMV", "VLUE", "SIZE"]) expect(text).toContain(etf);
    expect(text).toMatch(/not a Barra-style/);
    expect(text).toMatch(/not a Fama-French/);
  });

  it("describes the technical rating as descriptive", () => {
    const { container } = render(<MethodologyPage />);
    const text = container.querySelector("section#technical-rating")!.textContent ?? "";
    expect(text).toMatch(/descriptive/);
    expect(text).toMatch(/no claim about future returns/);
  });

  it("carries no advice, directive, or predictive vocabulary in the rendered text", () => {
    const { container } = render(<MethodologyPage />);
    const text = container.textContent ?? "";
    for (const pattern of BANNED) expect(text, String(pattern)).not.toMatch(pattern);
  });

  it("carries no advice, directive, or predictive vocabulary in its source", () => {
    const source = readFileSync(resolve(__dirname, "..", "app/methodology/page.tsx"), "utf8");
    for (const pattern of BANNED) expect(source, String(pattern)).not.toMatch(pattern);
  });
});

describe("How this is measured links", () => {
  it("points at the methodology anchor", () => {
    render(<MethodologyLink section="factor-risk" />);
    expect(screen.getByRole("link", { name: "How this is measured" })).toHaveAttribute(
      "href",
      "/methodology#factor-risk",
    );
  });

  for (const [file, section] of Object.entries(LINKED_PAGES)) {
    it(`${file} links to #${section}, an anchor that exists`, () => {
      const source = readFileSync(resolve(__dirname, "..", file), "utf8");
      expect(source).toContain(`<MethodologyLink section="${section}" />`);
      expect(ANCHORS).toContain(section);
    });
  }

  it("the footer links to the page", () => {
    const source = readFileSync(resolve(__dirname, "..", "app/layout.tsx"), "utf8");
    expect(source).toContain('href="/methodology"');
  });
});
