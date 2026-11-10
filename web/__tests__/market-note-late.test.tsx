// MarketNoteLate — the Holdings note after it missed the streamed window: it keeps fetching
// in the background and swaps the note in when it lands (Brian, 2026-11-09), and stops after
// a bounded number of tries.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen } from "@testing-library/react";

import type { MarketNote } from "@/lib/api";

const action = vi.hoisted(() => ({ fetchMarketNoteAction: vi.fn() }));
vi.mock("@/app/portfolios/[id]/market-note-action", () => action);

import { MarketNoteLate, NOTE_RETRY_DELAYS_MS } from "@/components/market-note-late";

const NOTE: MarketNote = {
  available: true,
  session_date: "2026-11-09",
  sentences: ["On Monday Nov 9 your portfolio rose 3.1% while the S&P 500 rose 2.4%, ahead by 0.7 points."],
  missing: [],
  portfolio_return: 0.031,
  lead_return: 0.024,
  gap: 0.007,
};

async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

describe("MarketNoteLate", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    action.fetchMarketNoteAction.mockReset();
  });
  afterEach(() => vi.useRealTimers());

  it("shows a working state, then the note once a retry succeeds", async () => {
    action.fetchMarketNoteAction
      .mockResolvedValueOnce({ ok: false, retry: true })
      .mockResolvedValueOnce({ ok: true, note: NOTE });
    render(<MarketNoteLate portfolioId="p1" accountIds={[]} />);
    expect(screen.getByText(/Still working on the note/)).toBeTruthy();
    await advance(NOTE_RETRY_DELAYS_MS[0]);
    await advance(NOTE_RETRY_DELAYS_MS[1]);
    expect(screen.getByText(/ahead by 0.7 points/)).toBeTruthy();
    expect(action.fetchMarketNoteAction).toHaveBeenCalledTimes(2);
  });

  it("gives up after the last try and says so", async () => {
    action.fetchMarketNoteAction.mockResolvedValue({ ok: false, retry: true });
    render(<MarketNoteLate portfolioId="p1" accountIds={[]} />);
    for (const d of NOTE_RETRY_DELAYS_MS) await advance(d);
    expect(action.fetchMarketNoteAction).toHaveBeenCalledTimes(NOTE_RETRY_DELAYS_MS.length);
    expect(screen.getByText(/The note did not load/)).toBeTruthy();
  });

  it("does not retry an error that will not change", async () => {
    action.fetchMarketNoteAction.mockResolvedValue({ ok: false, retry: false });
    render(<MarketNoteLate portfolioId="p1" accountIds={[]} />);
    await advance(NOTE_RETRY_DELAYS_MS[0]);
    await advance(60_000);
    expect(action.fetchMarketNoteAction).toHaveBeenCalledTimes(1);
  });
});
