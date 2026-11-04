// The Holdings market note (Brian, 2026-11-04): two or three plain sentences under the
// index + portfolio strip on why the portfolio moved differently from the markets. The
// sentences are built by the API from the same figures the strip shows; this only lays
// them out. Hidden, not shown broken, when the note is unavailable.

import type { MarketNote } from "@/lib/api";

export function MarketNoteView({ note }: { note: MarketNote | null }) {
  if (!note?.available || note.sentences.length === 0) return null;
  return (
    <div className="mt-3 rounded-lg border border-line p-4" aria-label="Why the portfolio moved">
      <div className="mb-1 text-xs uppercase tracking-wide text-muted">Portfolio vs market</div>
      <p className="text-sm leading-relaxed">{note.sentences.join(" ")}</p>
      {note.missing.length > 0 ? (
        <p className="mt-1 text-[11px] text-muted/70">Not included: {note.missing.join("; ")}</p>
      ) : null}
    </div>
  );
}
