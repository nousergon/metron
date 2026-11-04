// The Holdings market note (Brian, 2026-11-04): two or three plain sentences under the
// index + portfolio strip on why the portfolio moved differently from the markets. The
// sentences are built by the API from the same figures the strip shows; this only lays
// them out. When there is no note the card still shows and says why, because a hidden
// card reads as "not shipped" (Brian, 2026-11-04: "I'm not seeing it").

import type { MarketNote } from "@/lib/api";

const TITLE = "Portfolio vs market";

export function MarketNoteView({ note }: { note: MarketNote | null }) {
  const ready = note?.available && note.sentences.length > 0;
  const why = !note
    ? "The note could not be loaded. Reload the page to try again."
    : (note.missing[0] ?? "No note for this session yet.");
  return (
    <div className="mt-3 rounded-lg border border-line p-4" aria-label="Why the portfolio moved">
      <div className="mb-1 text-xs uppercase tracking-wide text-muted">{TITLE}</div>
      {ready ? (
        <>
          <p className="text-sm leading-relaxed">{note.sentences.join(" ")}</p>
          {note.missing.length > 0 ? (
            <p className="mt-1 text-[11px] text-muted/70">Not included: {note.missing.join("; ")}</p>
          ) : null}
        </>
      ) : (
        <p className="text-sm text-muted">{why}</p>
      )}
    </div>
  );
}
