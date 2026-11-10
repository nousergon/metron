"use client";

// The Holdings market note when it missed the page's streamed window (8s). It keeps asking
// in the background and swaps the note in when it lands, so the reader never has to reload
// for it (Brian, 2026-11-09). Gives up after a few tries and says so.

import { useEffect, useState } from "react";
import type { MarketNote } from "@/lib/api";
import { fetchMarketNoteAction } from "@/app/portfolios/[id]/market-note-action";
import { MarketNoteView } from "@/components/market-note";

export const NOTE_RETRY_DELAYS_MS = [2_000, 10_000, 30_000];

export function MarketNoteLate({ portfolioId, accountIds }: { portfolioId: string; accountIds: string[] }) {
  const [note, setNote] = useState<MarketNote | null>(null);
  const [gaveUp, setGaveUp] = useState(false);
  const key = accountIds.join(",");

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const attempt = (i: number) => {
      timer = setTimeout(async () => {
        const res = await fetchMarketNoteAction(portfolioId, accountIds).catch(() => ({ ok: false as const, retry: true }));
        if (cancelled) return;
        if (res.ok) setNote(res.note);
        else if (res.retry && i + 1 < NOTE_RETRY_DELAYS_MS.length) attempt(i + 1);
        else setGaveUp(true);
      }, NOTE_RETRY_DELAYS_MS[i]);
    };
    attempt(0);
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
    // accountIds is captured through `key`, which changes exactly when the selection does.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [portfolioId, key]);

  if (note) return <MarketNoteView note={note} />;
  return <MarketNoteView note={null} pending={!gaveUp} />;
}
