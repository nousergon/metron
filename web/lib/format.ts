// Display formatting helpers. Per-unit prices (avg cost, last price, FX) show to the
// cent via money(); portfolio AGGREGATES (cost basis, market value, unrealized, NAV,
// income) show whole dollars via moneyWhole() — cents are noise at portfolio magnitude
// and misalign columns. (metron-ops#45.) Quantities trim trailing zeros.

export function money(value: number, currency = "USD"): string {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency,
    maximumFractionDigits: 2,
  }).format(value);
}

/** Whole-dollar money for aggregates (no cents). Rounds to the nearest dollar. */
export function moneyWhole(value: number, currency = "USD"): string {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency,
    maximumFractionDigits: 0,
  }).format(value);
}

/** Signed money with a sign for non-zero values (gains/losses read at a glance). */
export function signedMoney(value: number, currency = "USD"): string {
  const formatted = money(Math.abs(value), currency);
  if (value > 0) return `+${formatted}`;
  if (value < 0) return `−${formatted}`; // minus sign, not hyphen
  return formatted;
}

/** Signed whole-dollar money for signed aggregates (unrealized/realized gains, flows). */
export function signedMoneyWhole(value: number, currency = "USD"): string {
  const formatted = moneyWhole(Math.abs(value), currency);
  if (value > 0) return `+${formatted}`;
  if (value < 0) return `−${formatted}`; // minus sign, not hyphen
  return formatted;
}

/** Accounting-style whole-dollar money: positive carries NO leading "+", losses are in
 *  parentheses (1,234 / (1,234)). Sign is read from color (signClass) + the parens, not a
 *  leading sign (metron-ops#80). */
export function accountingMoneyWhole(value: number, currency = "USD"): string {
  const formatted = moneyWhole(Math.abs(value), currency);
  return value < 0 ? `(${formatted})` : formatted;
}

/** Accounting-style money WITH cents (per-lot / native realized figures): no leading "+",
 *  losses in parentheses. The to-the-cent sibling of accountingMoneyWhole (metron-ops#80). */
export function accountingMoney(value: number, currency = "USD"): string {
  const formatted = money(Math.abs(value), currency);
  return value < 0 ? `(${formatted})` : formatted;
}

/** Accounting-style percentage from a decimal ratio: no leading "+", losses in
 *  parentheses (0.05 → "5.0%", -0.05 → "(5.0%)"). (metron-ops#80) */
export function accountingPercent(ratio: number): string {
  const pct = Math.abs(ratio * 100).toFixed(1);
  return ratio < 0 ? `(${pct}%)` : `${pct}%`;
}

export function quantity(value: number): string {
  return new Intl.NumberFormat("en-US", { maximumFractionDigits: 4 }).format(value);
}

/** Signed percentage from a decimal ratio (0.5 → "+50.0%"). */
export function percent(ratio: number): string {
  const pct = ratio * 100;
  const sign = pct > 0 ? "+" : pct < 0 ? "−" : "";
  return `${sign}${Math.abs(pct).toFixed(1)}%`;
}

/** FX rate display — significant digits, not fixed decimals, so small rates
 * stay meaningful (HKD→USD ≈ 0.1274) without padding rates near 1. */
export function fxRate(rate: number): string {
  return new Intl.NumberFormat("en-US", { maximumSignificantDigits: 4 }).format(rate);
}

/** Format a date-only ISO string (YYYY-MM-DD) without Date parsing, to avoid a
 * timezone day-shift (`new Date("2024-03-15")` is UTC midnight). */
export function isoDate(value: string): string {
  const [y, m, d] = value.split("-");
  if (!y || !m || !d) return value;
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return `${months[Number(m) - 1] ?? m} ${Number(d)}, ${y}`;
}

/** Tailwind text color for a gain/loss value. */
export function signClass(value: number): string {
  if (value > 0) return "text-positive";
  if (value < 0) return "text-negative";
  return "text-muted";
}

/** A valuation multiple (P/E, P/B, EV/EBITDA): "30.2×". */
export function multiple(value: number): string {
  return `${value.toFixed(1)}×`;
}

/** A large money amount in human units: "$3.0T" / "$450.2B" / "$12.3B" / "$840.0M".
 * Handles negatives (net debt / FCF can be negative) as "−$12.3B". */
export function marketCapShort(value: number, currency = "USD"): string {
  const abs = Math.abs(value);
  const [div, suffix] = abs >= 1e12 ? [1e12, "T"] : abs >= 1e9 ? [1e9, "B"] : abs >= 1e6 ? [1e6, "M"] : [1e3, "K"];
  const sym = currency === "USD" ? "$" : "";
  const sign = value < 0 ? "−" : "";
  return `${sign}${sym}${(abs / div).toFixed(1)}${suffix}`;
}

/** A plain ratio/level (current ratio, beta, RSI): fixed decimals, no unit. */
export function decimal(value: number, places = 2): string {
  return value.toFixed(places);
}

/** Unsigned percentage from a decimal ratio (0.45 → "45.0%") — for margins/yield/range
 * where the sign isn't meaningful (unlike `percent`, which is signed for growth/returns). */
export function pct1(ratio: number): string {
  return `${(ratio * 100).toFixed(1)}%`;
}
