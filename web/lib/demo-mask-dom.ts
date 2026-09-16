// The DOM half of the demo currency mask (metron-ops-I305 deliverable 2).
// Pure functions over a supplied root node — no React, no globals — so the "masks EVERY
// currency cell" claim is testable over arbitrary rendered markup rather than over one
// component's props. See lib/demo-mask.ts for why the mask is a DOM pass.

import { MASKED_ATTRIBUTES, currencyAmounts, hasCurrency, maskCurrencyText } from "@/lib/demo-mask";

/** Elements whose text is never user-visible; masking them is wasted work and editing a
 *  <script>/<style> body could change behaviour rather than presentation. */
const SKIPPED_TAGS = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE"]);

export interface MaskResult {
  /** The percentage base used (largest absolute amount seen, monotonic across passes). */
  base: number;
  /** How many text nodes and attributes were rewritten in this pass. */
  replacements: number;
}

function textNodes(root: Node): Text[] {
  const doc = root.ownerDocument ?? (root as Document);
  const walker = doc.createTreeWalker(root, 4 /* NodeFilter.SHOW_TEXT */, {
    acceptNode(node: Node) {
      const parent = node.parentElement;
      if (parent && SKIPPED_TAGS.has(parent.tagName)) return 2 /* FILTER_REJECT */;
      return 1 /* FILTER_ACCEPT */;
    },
  });
  const out: Text[] = [];
  let n = walker.nextNode();
  while (n) {
    out.push(n as Text);
    n = walker.nextNode();
  }
  return out;
}

function elements(root: Node): Element[] {
  const out: Element[] = [];
  if (root instanceof Element) out.push(root);
  const scope = root instanceof Element || root instanceof Document || root instanceof DocumentFragment ? root : null;
  if (scope) out.push(...Array.from(scope.querySelectorAll("*")));
  return out;
}

/** The largest absolute currency amount rendered under `root`, or 0 if there is none. */
export function observedBase(root: Node): number {
  let max = 0;
  for (const node of textNodes(root)) {
    for (const amount of currencyAmounts(node.data)) max = Math.max(max, Math.abs(amount));
  }
  for (const el of elements(root)) {
    for (const attr of MASKED_ATTRIBUTES) {
      const v = el.getAttribute(attr);
      if (v) for (const amount of currencyAmounts(v)) max = Math.max(max, Math.abs(amount));
    }
  }
  return max;
}

/**
 * Rewrite every currency amount under `root` as a percentage of the base.
 *
 * `priorBase` keeps the base monotonic across passes: content that streams in later
 * (SWR revalidation, a lazily-mounted panel) must not re-scale the amounts already on
 * screen. Returns the base in force and the number of replacements made — zero
 * replacements on a page that had none is a legitimate result, and the caller
 * distinguishes "nothing to mask" from "mask never ran" by the applied attribute.
 */
export function applyMask(root: Node, priorBase = 0): MaskResult {
  const base = Math.max(priorBase, observedBase(root));
  let replacements = 0;

  for (const node of textNodes(root)) {
    if (!hasCurrency(node.data)) continue;
    node.data = maskCurrencyText(node.data, base);
    replacements += 1;
  }

  for (const el of elements(root)) {
    for (const attr of MASKED_ATTRIBUTES) {
      const v = el.getAttribute(attr);
      if (!v || !hasCurrency(v)) continue;
      el.setAttribute(attr, maskCurrencyText(v, base));
      replacements += 1;
    }
  }

  return { base, replacements };
}

/** Every string still showing a currency amount under `root`. Empty is the pass
 *  condition the recorder and the mask test both assert on. */
export function residualCurrency(root: Node): string[] {
  const out: string[] = [];
  for (const node of textNodes(root)) {
    if (hasCurrency(node.data)) out.push(node.data.trim());
  }
  for (const el of elements(root)) {
    for (const attr of MASKED_ATTRIBUTES) {
      const v = el.getAttribute(attr);
      if (v && hasCurrency(v)) out.push(`${attr}="${v}"`);
    }
  }
  return out;
}
