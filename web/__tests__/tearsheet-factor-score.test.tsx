// Tearsheet "Factor score" gauge (metron-ops#106) — metron-ops-I334: a stale score renders an
// explicit "stale" tag beside the number, a retired substrate renders an explicit retired
// state (the section never silently vanishes), and a coverage gap stays hidden — three
// visually distinct states.

import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import { TearsheetFactorScore } from "@/components/tearsheet-factor-score";
import type { TearsheetAttractiveness } from "@/lib/api";

const gauge = (over: Partial<TearsheetAttractiveness> = {}): TearsheetAttractiveness => ({
  available: true,
  score: 72.4,
  coverage: 6,
  as_of: "2026-09-10",
  components: [{ key: "quality", weight: 0.2, score: 90, contribution: null }],
  stale: false,
  retired: false,
  ...over,
});

describe("TearsheetFactorScore", () => {
  it("renders a fresh score with its pillar breakdown and no stale tag", () => {
    render(<TearsheetFactorScore attractiveness={gauge()} />);
    expect(screen.getByText("Factor score")).toBeInTheDocument();
    expect(screen.getByText("72.4")).toBeInTheDocument();
    expect(screen.getByText("Quality")).toBeInTheDocument();
    expect(screen.queryByText("stale")).not.toBeInTheDocument();
  });

  it("tags a stale score 'stale' beside the number", () => {
    const { container } = render(<TearsheetFactorScore attractiveness={gauge({ stale: true })} />);
    expect(screen.getByText("72.4")).toBeInTheDocument();
    const tag = container.querySelector<HTMLElement>('[data-factor-state="stale"]');
    expect(tag?.textContent).toBe("stale");
    expect(tag?.title).toMatch(/more than 8 days old/);
  });

  it("renders an explicit retired state once the substrate is retired", () => {
    const { container } = render(
      <TearsheetFactorScore
        attractiveness={gauge({ available: false, score: null, coverage: null, as_of: null, components: [], retired: true })}
      />,
    );
    expect(screen.getByText("Factor score")).toBeInTheDocument();
    expect(screen.getByText("retired")).toBeInTheDocument();
    expect(container.querySelector('[data-factor-state="retired"]')?.textContent).toMatch(/retired/i);
    expect(screen.queryByText("stale")).not.toBeInTheDocument();
  });

  it("renders nothing for a coverage gap (not stale, not retired)", () => {
    const { container } = render(
      <TearsheetFactorScore attractiveness={gauge({ available: false, score: null, components: [] })} />,
    );
    expect(container).toBeEmptyDOMElement();
  });
});
