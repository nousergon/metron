// Holding period label (Brian 2026-11-04).
import { describe, expect, it } from "vitest";
import { heldLabel } from "@/components/holdings-table";

describe("heldLabel", () => {
  it("reads days, then months, then years", () => {
    expect(heldLabel(12)).toBe("12d");
    expect(heldLabel(240)).toBe("8mo");
    expect(heldLabel(1170)).toBe("3.2y");
  });
});
