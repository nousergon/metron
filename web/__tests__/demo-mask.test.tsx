// The demo currency mask must leave NO currency on the page (metron-ops-I305
// deliverable 2). The load-bearing assertion is the sweep at the end of the first
// block: a fixture built from every money formatter this app owns, rendered, masked,
// then scanned for any residual currency — so a new formatter that emits a shape the
// mask does not recognise fails here rather than in a published recording.

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DemoMask } from "@/components/demo-mask";
import { DEMO_MASK_APPLIED_ATTR, DEMO_MASK_COOKIE, hasCurrency, maskCurrencyText, maskFromCookie, maskFromSearch } from "@/lib/demo-mask";
import { applyMask, observedBase, residualCurrency } from "@/lib/demo-mask-dom";
import {
  accountingMoney,
  accountingMoneyWhole,
  marketCapShort,
  money,
  moneyWhole,
  signedMoney,
  signedMoneyWhole,
} from "@/lib/format";

/** Every currency-emitting formatter in lib/format.ts, exercised over positive,
 *  negative and zero values plus a non-USD currency. */
function currencyFixtures(): string[] {
  const values = [1_284_930.55, 12_345, 987.4, 0, -4_210.75, -19];
  const out: string[] = [];
  for (const v of values) {
    out.push(money(v), moneyWhole(v), signedMoney(v), signedMoneyWhole(v), accountingMoney(v), accountingMoneyWhole(v), marketCapShort(v));
  }
  out.push(money(1234.5, "EUR"), moneyWhole(98_765, "GBP"), marketCapShort(3.02e12), marketCapShort(4.5e8));
  return out;
}

describe("demo mask — every currency cell", () => {
  it("leaves no currency anywhere in a document built from every money formatter", () => {
    const host = document.createElement("div");
    host.innerHTML = `
      <section>
        <h1>Portfolio</h1>
        ${currencyFixtures().map((s, i) => `<div class="cell"><span data-testid="c${i}">${s}</span></div>`).join("")}
        <table>
          <tbody>
            <tr><td title="Market value ${money(1_284_930.55)}">AAPL</td><td aria-label="Unrealized ${signedMoney(-4_210.75)}">x</td></tr>
          </tbody>
        </table>
        <p>Cash of $5,000 was deployed; the remaining $312.50 stayed unallocated.</p>
        <script>var priceLabel = "$999.99";</script>
      </section>`;
    document.body.appendChild(host);

    const { base, replacements } = applyMask(host);

    expect(base).toBe(3e12); // the largest amount rendered ("$3.0T") drives the base
    expect(replacements).toBeGreaterThan(0);
    expect(residualCurrency(host)).toEqual([]);
    // Same claim stated over the raw visible text, independent of the walker above.
    const visible = host.cloneNode(true) as HTMLElement;
    for (const s of Array.from(visible.querySelectorAll("script,style"))) s.remove();
    expect(hasCurrency(visible.textContent ?? "")).toBe(false);

    // The <script> body is deliberately untouched: masking it would change behaviour,
    // and it is not a rendered cell.
    expect(host.querySelector("script")?.textContent).toContain("$999.99");

    document.body.removeChild(host);
  });

  it("preserves sign and accounting parentheses, and never invents a number", () => {
    expect(maskCurrencyText("+$1,000", 10_000)).toBe("+10.0%");
    expect(maskCurrencyText("−$2,500", 10_000)).toBe("−25.0%");
    expect(maskCurrencyText("($2,500)", 10_000)).toBe("(25.0%)");
    expect(maskCurrencyText("$3.0T", 3e12)).toBe("100.0%");
    // No usable base -> a placeholder, not a 0% or an Infinity%.
    expect(maskCurrencyText("$1,000", 0)).toBe("••");
    expect(maskCurrencyText("$1,000", Number.NaN)).toBe("••");
  });

  it("does not touch quantities, ratios, dates or percentages", () => {
    const untouched = "120 shares · 4.35% yield · 30.2× · RSI 61.4 · Mar 15, 2024 · 1,284,930";
    expect(maskCurrencyText(untouched, 10_000)).toBe(untouched);
  });

  it("keeps the base monotonic so late-arriving content cannot re-scale what is on screen", () => {
    const host = document.createElement("div");
    host.innerHTML = `<span>${moneyWhole(1_000)}</span>`;
    const first = applyMask(host, 0);
    expect(first.base).toBe(1_000);
    expect(host.textContent).toBe("100.0%");

    const late = document.createElement("span");
    late.textContent = moneyWhole(4_000);
    host.appendChild(late);
    const second = applyMask(host, first.base);
    expect(second.base).toBe(4_000);
    expect(host.querySelector("span")?.textContent).toBe("100.0%"); // unchanged
    expect(late.textContent).toBe("100.0%"); // 4,000 of a 4,000 base
    expect(residualCurrency(host)).toEqual([]);
  });

  it("reads the flag from the query string, then the cookie", () => {
    expect(maskFromSearch("?demo_mask=1")).toBe(true);
    expect(maskFromSearch("?demo_mask=0")).toBe(false);
    expect(maskFromSearch("?other=1")).toBeNull();
    expect(maskFromCookie(`a=b; ${DEMO_MASK_COOKIE}=1; c=d`)).toBe(true);
    expect(maskFromCookie("a=b")).toBe(false);
  });

  it("observedBase reports 0 for a page with no currency at all", () => {
    const host = document.createElement("div");
    host.innerHTML = "<p>No amounts here — 42 holdings.</p>";
    expect(observedBase(host)).toBe(0);
  });
});

describe("DemoMask component", () => {
  it("masks the live document and marks the pass when the cookie is set", async () => {
    document.cookie = `${DEMO_MASK_COOKIE}=1; path=/`;
    render(
      <div>
        <span data-testid="nav">{moneyWhole(250_000)}</span>
        <span data-testid="pos">{moneyWhole(25_000)}</span>
        <DemoMask />
      </div>,
    );

    expect(document.documentElement.getAttribute(DEMO_MASK_APPLIED_ATTR)).toBe("1");
    expect(screen.getByTestId("nav").textContent).toBe("100.0%");
    expect(screen.getByTestId("pos").textContent).toBe("10.0%");
    document.cookie = `${DEMO_MASK_COOKIE}=; path=/; Max-Age=0`;
  });

  it("is inert with no flag — the ordinary app renders real amounts", () => {
    render(
      <div>
        <span data-testid="nav">{moneyWhole(250_000)}</span>
        <DemoMask />
      </div>,
    );

    expect(screen.getByTestId("nav").textContent).toBe("$250,000");
    expect(document.documentElement.getAttribute(DEMO_MASK_APPLIED_ATTR)).toBeNull();
  });
});
