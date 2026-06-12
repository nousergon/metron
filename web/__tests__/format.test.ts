// Display formatting — the money/sign conventions every table renders through.

import { describe, expect, it } from "vitest";
import { money, percent, signClass, signedMoney } from "@/lib/format";

describe("format", () => {
  it("money renders currency to the cent", () => {
    expect(money(1234.5)).toBe("$1,234.50");
    expect(money(1234.5, "HKD")).toMatch(/1,234\.50/);
  });

  it("signedMoney uses an explicit + and a true minus sign", () => {
    expect(signedMoney(200)).toBe("+$200.00");
    expect(signedMoney(-200)).toBe("−$200.00"); // U+2212, not hyphen
    expect(signedMoney(0)).toBe("$0.00");
  });

  it("signClass maps sign to the P&L color tokens", () => {
    expect(signClass(5)).toContain("positive");
    expect(signClass(-5)).toContain("negative");
  });

  it("percent renders a ratio as a percentage", () => {
    expect(percent(0.1234)).toMatch(/12\.3/);
  });
});
