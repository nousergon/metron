// DiagnosticsCard (metron-ops-I167) — deterministic structure FACTS: concentration
// stat tiles + static explainer legend; benchmark columns degrade honestly when the
// source has nothing; the user-target drift section renders ONLY when the user has
// authored targets (target_drift null = no section).

import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import { DiagnosticsCard } from "@/components/diagnostics-card";
import type { Diagnostics } from "@/lib/api";

const base: Diagnostics = {
  computable: true,
  reason: null,
  required_tier: null,
  as_of: "2026-07-08",
  base_currency: "USD",
  total_market_value: 1500,
  benchmark: "SPY",
  benchmark_available: true,
  benchmark_reason: null,
  benchmark_required_tier: null,
  concentration: {
    n_positions: 2,
    hhi: 0.5556,
    effective_n: 1.8,
    top5_share: 1.0,
    top10_share: 1.0,
    max_position_ticker: "AAPL",
    max_position_weight: 0.6667,
  },
  sectors: [
    { sector: "Technology", weight: 0.6667, market_value: 1000, benchmark_weight: 0.4, delta: 0.2667 },
    { sector: "Energy", weight: 0.3333, market_value: 500, benchmark_weight: 0.05, delta: 0.2833 },
  ],
  geography: [
    { bucket: "US", weight: 0.6667, market_value: 1000 },
    { bucket: "International", weight: 0.3333, market_value: 500 },
  ],
  target_drift: null,
};

describe("DiagnosticsCard", () => {
  it("renders concentration metrics with the static explainer legend", () => {
    render(<DiagnosticsCard d={base} />);
    // "HHI" appears in both the stat tile and the explainer legend.
    expect(screen.getAllByText("HHI").length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText("0.556")).toBeInTheDocument();
    expect(screen.getByText("≈ 1.8 effective positions")).toBeInTheDocument();
    // Static explainer copy (never generated).
    expect(screen.getByText("What these measure")).toBeInTheDocument();
    expect(screen.getByText(/Herfindahl–Hirschman index/)).toBeInTheDocument();
  });

  it("shows benchmark deltas when available", () => {
    render(<DiagnosticsCard d={base} />);
    expect(screen.getByText("vs SPY sector weights")).toBeInTheDocument();
    expect(screen.getByText("+26.7%")).toBeInTheDocument(); // Technology overweight
  });

  it("degrades honestly when benchmark weights are pending", () => {
    const d: Diagnostics = {
      ...base,
      benchmark_available: false,
      benchmark_reason: "unavailable",
      sectors: base.sectors.map((s) => ({ ...s, benchmark_weight: null, delta: null })),
    };
    render(<DiagnosticsCard d={d} />);
    expect(screen.getByText(/benchmark weights pending/)).toBeInTheDocument();
    // Portfolio-side weights still shown; deltas render as em-dashes, never fabricated.
    expect(screen.getByText("Technology")).toBeInTheDocument();
  });

  it("renders NO target section when the user has authored no targets", () => {
    render(<DiagnosticsCard d={base} />);
    expect(screen.queryByText("Your stated targets")).not.toBeInTheDocument();
  });

  it("renders the target drift rows mechanically when targets exist", () => {
    const d: Diagnostics = {
      ...base,
      target_drift: [
        { kind: "max_position", label: "AAPL", target: 0.1, actual: 0.6667, breach: true, detail: null },
        { kind: "avoid_sector", label: "Energy", target: 0, actual: 0.3333, breach: true, detail: "XOM" },
      ],
    };
    render(<DiagnosticsCard d={d} />);
    expect(screen.getByText("Your stated targets")).toBeInTheDocument();
    expect(screen.getByText("Max single position — AAPL")).toBeInTheDocument();
    expect(screen.getByText("above your stated max")).toBeInTheDocument();
    expect(screen.getByText("Avoid sector — Energy")).toBeInTheDocument();
    expect(screen.getByText("held: XOM")).toBeInTheDocument();
  });
});
