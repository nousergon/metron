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

/** Tailwind text color for a gain/loss value. */
export function signClass(value: number): string {
  if (value > 0) return "text-positive";
  if (value < 0) return "text-negative";
  return "text-muted";
}
