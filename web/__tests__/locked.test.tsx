// Locked — the full-page placeholder shown when a gated route is navigated to directly
// (Phase 2b of the tier simulator). The copy keys off the entitlement reason: "tier"
// → the plan doesn't include it; a data reason ("feed"/"benchmark"/"etf_vendor") → it
// needs the licensed market-data feed. Either way it names the upsell tier.

import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import { Locked } from "@/components/ui";

describe("Locked", () => {
  it("tier-excluded feature names the plan, not the feed", () => {
    render(<Locked label="Factor risk" reason="tier" requiredTier="pro" />);
    expect(screen.getByRole("heading", { name: "Factor risk" })).toBeInTheDocument();
    expect(screen.getByText(/is part of the/)).toHaveTextContent("Pro");
    expect(screen.queryByText(/market-data feed/)).not.toBeInTheDocument();
  });

  it("feed-excluded feature explains the licensed market-data feed", () => {
    render(<Locked label="Factor risk" reason="feed" requiredTier="pro" />);
    expect(screen.getByText(/needs the licensed market-data feed/)).toHaveTextContent("Pro");
  });

  it("maps required-tier keys to display labels (agentic → Research+)", () => {
    render(<Locked label="Agentic quant research" reason="tier" requiredTier="agentic" />);
    expect(screen.getByText(/is part of the/)).toHaveTextContent("Research+");
  });

  it("benchmark/etf_vendor reasons are treated as data (feed) exclusions", () => {
    render(<Locked label="ETF look-through" reason="etf_vendor" requiredTier="pro" />);
    expect(screen.getByText(/needs the licensed market-data feed/)).toBeInTheDocument();
  });
});
