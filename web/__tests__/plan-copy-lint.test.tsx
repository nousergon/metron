// Copy-lint (metron-ops-I311, intelligence-doctrine layer 2) — the plan surface must
// never read as advice: no directive vocabulary anywhere in its component source. A
// source-text scan rather than a DOM scan on purpose — it catches banned copy sitting in
// an untriggered branch (an error string, a future variant) that a render-only test would
// never reach.

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const FILES = [
  "components/plan-targets-form.tsx",
  "components/cash-to-targets-panel.tsx",
  "components/whatif-purchase-panel.tsx",
  "app/portfolios/[id]/plan/page.tsx",
  "app/portfolios/[id]/planning-actions.ts",
];

const BANNED = [/recommend/i, /should buy/i, /\badvice\b/i];

describe("plan surface copy lint", () => {
  for (const file of FILES) {
    it(`${file} carries no directive vocabulary`, () => {
      const text = readFileSync(resolve(__dirname, "..", file), "utf8");
      for (const pattern of BANNED) {
        expect(pattern.test(text)).toBe(false);
      }
    });
  }
});
