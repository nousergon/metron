// Display formatting — the money/sign conventions every table renders through.

import { describe, expect, it } from "vitest";
import { money, moneyWhole, percent, signClass, signedMoney, signedMoneyWhole } from "@/lib/format";

describe("format", () => {
  it("money renders currency to the cent", () => {
    expect(money(1234.5)).toBe("$1,234.50");
    expect(money(1234.5, "HKD")).toMatch(/1,234\.50/);
  });

  it("moneyWhole renders aggregates with no cents (rounded)", () => {
    expect(moneyWhole(1234.5)).toBe("$1,235"); // Intl default rounding: half away from zero
    expect(moneyWhole(1234.4)).toBe("$1,234");
    expect(moneyWhole(1000000)).toBe("$1,000,000");
    expect(moneyWhole(1234.5, "HKD")).toMatch(/1,23[45]/); // no decimal places
    expect(moneyWhole(1234.99)).not.toMatch(/\./); // never shows a decimal point
  });

  it("signedMoney uses an explicit + and a true minus sign", () => {
    expect(signedMoney(200)).toBe("+$200.00");
    expect(signedMoney(-200)).toBe("−$200.00"); // U+2212, not hyphen
    expect(signedMoney(0)).toBe("$0.00");
  });

  it("signedMoneyWhole signs whole-dollar aggregates", () => {
    expect(signedMoneyWhole(200.4)).toBe("+$200");
    expect(signedMoneyWhole(-200.6)).toBe("−$201"); // U+2212, rounded
    expect(signedMoneyWhole(0)).toBe("$0");
  });

  it("signClass maps sign to the P&L color tokens", () => {
    expect(signClass(5)).toContain("positive");
    expect(signClass(-5)).toContain("negative");
  });

  it("percent renders a ratio as a percentage", () => {
    expect(percent(0.1234)).toMatch(/12\.3/);
  });
});
