// RetryRefresh — a failed streamed section re-renders the page in the background a bounded
// number of times instead of asking the reader to reload (Brian, 2026-11-09).

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render } from "@testing-library/react";

const refresh = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh }) }));

import { RETRY_DELAYS_MS, RetryRefresh, RetryReset } from "@/components/retry-refresh";

describe("RetryRefresh", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    refresh.mockReset();
    window.sessionStorage.clear();
  });
  afterEach(() => vi.useRealTimers());

  it("refreshes after the first delay", () => {
    render(<RetryRefresh retryKey="holdings:p1" />);
    vi.advanceTimersByTime(RETRY_DELAYS_MS[0] - 1);
    expect(refresh).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("stops once the retry budget is spent", () => {
    window.sessionStorage.setItem("metron-retry:holdings:p1", String(RETRY_DELAYS_MS.length));
    render(<RetryRefresh retryKey="holdings:p1" />);
    vi.advanceTimersByTime(120_000);
    expect(refresh).not.toHaveBeenCalled();
  });

  it("a successful render resets the budget", () => {
    window.sessionStorage.setItem("metron-retry:holdings:p1", "3");
    render(<RetryReset retryKey="holdings:p1" />);
    expect(window.sessionStorage.getItem("metron-retry:holdings:p1")).toBeNull();
  });
});
