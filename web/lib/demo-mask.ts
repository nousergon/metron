// Demo currency mask (metron-ops-I305 deliverable 2).
//
// WHY THIS EXISTS. The Stage A demo recording walks Brian's OWN build, and one step of
// the runbook (`metron-ops/docs/runbooks/metron-demo-path.md` §1, step 4b) shows his
// personal portfolio. The recording is published; his balances are not. `?demo_mask=1`
// is the render flag that makes that step publishable: every currency amount on the
// page is replaced by its share of the largest amount on the page, so the SHAPE of the
// portfolio survives and the DOLLARS do not.
//
// WHY IT IS A DOM PASS AND NOT A FORMATTER FLAG. `lib/format.ts` is pure and is called
// from both server and client components, so it has no per-request place to read a flag
// from without either a server-global (leaks across concurrent requests) or an edit to
// every call site. More importantly, a formatter flag masks only the amounts that go
// THROUGH the formatter — a hard-coded string, an API-supplied label, a `title`
// attribute or a chart tooltip would each render an unmasked figure and nothing would
// say so. The requirement is EVERY currency cell, so the mask runs over what was
// actually rendered. The functions here are pure; `components/demo-mask.tsx` drives
// them over the live document.
//
// PERCENTAGE BASE. Amounts are expressed against the largest absolute amount observed
// so far in the session (in practice the portfolio total). The base is monotonic: once
// a larger amount is seen it becomes the base, and amounts already masked keep the
// percentage they were given. This is a redaction, not a measurement — nothing
// downstream reads these percentages.

/** Query parameter that turns the mask on (`?demo_mask=1`) or off (`?demo_mask=0`). */
export const DEMO_MASK_PARAM = "demo_mask";

/** Cookie the flag persists into, so the mask survives navigation during a recording. */
export const DEMO_MASK_COOKIE = "metron-demo-mask";

/** Set on <html> once a mask pass has completed — the recorder waits on it. */
export const DEMO_MASK_APPLIED_ATTR = "data-demo-mask-applied";

/** Rendered when there is no usable base (no positive amount seen yet). */
export const MASK_PLACEHOLDER = "••";

// Currency amounts as this app renders them (lib/format.ts): an optional leading sign
// (+, -, − or an opening paren), a currency symbol, grouped digits, optional decimals,
// an optional magnitude suffix (marketCapShort emits $3.0T / $450.2B / $12.3M / $840.0K)
// and an optional closing paren. The symbol is required: a bare number is a quantity, a
// ratio or a count, and masking those would destroy the page for no privacy gain.
const CURRENCY_SOURCE = String.raw`([+\-−(]?)\s*([$€£¥₹])\s*(\d[\d,]*(?:\.\d+)?)\s*([KMBT])?(\))?`;

/** Global matcher over rendered text. A fresh RegExp per call — a shared /g/ instance
 *  carries `lastIndex` between calls and would skip matches. */
export function currencyPattern(): RegExp {
  return new RegExp(CURRENCY_SOURCE, "g");
}

/** True when `text` still contains a renderable currency amount. The recorder and the
 *  tests both assert the negation of this over the whole document. */
export function hasCurrency(text: string): boolean {
  return currencyPattern().test(text);
}

/** Every currency amount in `text`, as absolute numbers (suffixes expanded). */
export function currencyAmounts(text: string): number[] {
  const out: number[] = [];
  const re = currencyPattern();
  let m: RegExpExecArray | null = re.exec(text);
  while (m !== null) {
    out.push(expand(m[3], m[4]));
    m = re.exec(text);
  }
  return out;
}

const SUFFIX_FACTOR: Record<string, number> = { K: 1e3, M: 1e6, B: 1e9, T: 1e12 };

function expand(digits: string, suffix: string | undefined): number {
  const n = Number(digits.replace(/,/g, ""));
  if (!Number.isFinite(n)) return 0;
  return suffix ? n * (SUFFIX_FACTOR[suffix] ?? 1) : n;
}

/**
 * Replace every currency amount in `text` with its percentage of `base`.
 *
 * Sign and accounting parentheses are preserved, because they carry meaning the viewer
 * is meant to read (a loss still reads as a loss). `base <= 0` or a non-finite base
 * yields the placeholder rather than a nonsense percentage — masking must never invent
 * a number.
 */
export function maskCurrencyText(text: string, base: number): string {
  return text.replace(currencyPattern(), (_all, sign: string, _sym: string, digits: string, suffix: string | undefined, close: string | undefined) => {
    const open = sign === "(";
    const amount = expand(digits, suffix);
    const body = base > 0 && Number.isFinite(base) ? `${((amount / base) * 100).toFixed(1)}%` : MASK_PLACEHOLDER;
    if (open) return `(${body}${close ?? ")"}`;
    const lead = sign === "+" || sign === "-" || sign === "−" ? sign : "";
    return `${lead}${body}${close ?? ""}`;
  });
}

/**
 * Read the mask flag out of a query string.
 *
 * Returns `true` for `?demo_mask=1`, `false` for `?demo_mask=0` (an explicit un-mask,
 * so a recording can be resumed unmasked without clearing cookies), and `null` when the
 * parameter is absent — the caller then falls back to the cookie.
 */
export function maskFromSearch(search: string): boolean | null {
  const raw = new URLSearchParams(search.startsWith("?") ? search.slice(1) : search).get(DEMO_MASK_PARAM);
  if (raw === null) return null;
  return raw === "1" || raw.toLowerCase() === "true";
}

/** Read the mask flag out of a `document.cookie`-shaped string. */
export function maskFromCookie(cookie: string): boolean {
  for (const part of cookie.split(";")) {
    const [k, ...rest] = part.trim().split("=");
    if (k === DEMO_MASK_COOKIE) return rest.join("=") === "1";
  }
  return false;
}

/** Attributes that can carry a currency amount into the rendered page without passing
 *  through a text node (tooltips, accessible names, chart labels). */
export const MASKED_ATTRIBUTES = ["title", "aria-label", "aria-valuetext", "alt", "data-value"] as const;
