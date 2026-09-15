"use client";

import { useState, useTransition } from "react";
import type { LockedCard } from "@/lib/api";
import { recordLockedCardTapAction } from "@/app/portfolios/[id]/locked-card-action";

// Locked cards (metron-ops-I310): one card per feature that exists but is not available
// in the external demo. A tap is counted first-party (the demand signal per feature) and
// acknowledged in place; nothing else happens.
export function LockedCards({ cards }: { cards: LockedCard[] }) {
  const [tapped, setTapped] = useState<Set<string>>(new Set());
  const [, start] = useTransition();

  function onTap(key: string) {
    setTapped((prev) => new Set(prev).add(key));
    start(async () => {
      await recordLockedCardTapAction(key);
    });
  }

  return (
    <ul className="mt-4 space-y-3">
      {cards.map((c) => (
        <li key={c.key}>
          <button
            type="button"
            onClick={() => onTap(c.key)}
            aria-pressed={tapped.has(c.key)}
            className="w-full rounded-lg border border-dashed border-line p-4 text-left transition hover:bg-white/5"
          >
            <span className="flex items-center justify-between gap-2">
              <span className="text-sm font-medium text-ink">{c.name}</span>
              <span aria-hidden="true">🔒</span>
            </span>
            <span className="mt-1 block text-sm text-muted">{c.line}</span>
            <span className="mt-2 block text-[11px] uppercase tracking-[0.12em] text-muted/70">
              {tapped.has(c.key) ? "Interest noted" : c.status}
            </span>
          </button>
        </li>
      ))}
    </ul>
  );
}
