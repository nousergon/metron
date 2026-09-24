// Transactions-table type cell (metron-ops#335): a dividend reinvestment renders as a
// reinvested dividend, distinct from a plain buy; every other type renders unchanged.

import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import {
  REINVESTMENT_LABEL,
  REINVESTMENT_TITLE,
  TxnTypeLabel,
} from "@/components/txn-type-label";

describe("TxnTypeLabel", () => {
  it("renders REINVESTMENT as a labelled reinvested-dividend badge, not a buy", () => {
    render(<TxnTypeLabel type="REINVESTMENT" />);
    const badge = screen.getByText(REINVESTMENT_LABEL);
    expect(badge).toBeInTheDocument();
    expect(badge.title).toBe(REINVESTMENT_TITLE);
    expect(badge.getAttribute("data-txn-type")).toBe("REINVESTMENT");
    expect(screen.queryByText("BUY")).not.toBeInTheDocument();
    expect(screen.queryByText("REINVESTMENT")).not.toBeInTheDocument();
  });

  it("describes what happened without implying a recommendation", () => {
    const copy = `${REINVESTMENT_LABEL} ${REINVESTMENT_TITLE}`.toLowerCase();
    expect(copy).toContain("dividend");
    for (const word of ["should", "recommend", "consider", "advice", "suggest"]) {
      expect(copy).not.toContain(word);
    }
  });

  it.each(["BUY", "SELL", "DIVIDEND", "INTEREST", "DEPOSIT", "WITHDRAWAL", "FEE", "SPLIT"])(
    "renders %s as its raw canonical value, unchanged",
    (type) => {
      const { container } = render(<TxnTypeLabel type={type} />);
      expect(container.textContent).toBe(type);
      expect(container.querySelector("[data-txn-type]")).toBeNull();
    },
  );
});
