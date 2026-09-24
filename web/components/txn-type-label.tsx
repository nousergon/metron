// Transaction-type cell for the Transactions tables (Tax page, account detail).
//
// A dividend reinvestment (DRP/DRIP) is stored as its own ledger type, REINVESTMENT
// (metron-ops#335). Economically it is a buy funded by a dividend and every calculation
// treats it as one; the only thing that differs is how it reads here, so a reader can
// see "these shares came from a dividend" instead of an unexplained buy. The copy
// describes what happened and nothing else — no suggestion about what to do.
// Every other type renders exactly as before: the raw canonical value.

export const REINVESTMENT = "REINVESTMENT";

export const REINVESTMENT_LABEL = "Reinvested dividend";

export const REINVESTMENT_TITLE =
  "Shares bought automatically with a dividend paid on this holding (dividend reinvestment). " +
  "The dividend itself is listed as its own row on the same date.";

export function TxnTypeLabel({ type }: { type: string }) {
  if (type === REINVESTMENT) {
    return (
      <span
        className="inline-block rounded border border-line px-1.5 py-0.5 text-xs text-sky-500"
        title={REINVESTMENT_TITLE}
        data-txn-type={type}
      >
        {REINVESTMENT_LABEL}
      </span>
    );
  }
  return <>{type}</>;
}
