// Display formatting helpers. Money is shown to the cent; quantities trim trailing
// zeros (fractional shares are real but 6.0000 reads as noise).

export function money(value: number, currency = "USD"): string {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency,
    maximumFractionDigits: 2,
  }).format(value);
}

/** Signed money with a sign for non-zero values (gains/losses read at a glance). */
export function signedMoney(value: number, currency = "USD"): string {
  const formatted = money(Math.abs(value), currency);
  if (value > 0) return `+${formatted}`;
  if (value < 0) return `−${formatted}`; // minus sign, not hyphen
  return formatted;
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
