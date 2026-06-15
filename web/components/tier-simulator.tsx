"use client";

// Owner-only product-tier simulator: preview how each Metron product level
// (Beta / Pro / Research+ / Base) looks, and toggle the market-data feed to SEE
// what the no-feed beta excludes. Renders NOTHING unless the server reports
// `simulator: true` (settings.tier_simulator) — it never appears on the public
// product. Selections persist in cookies the server reads on the next render;
// `router.refresh()` re-fetches the gated server components.

import { useTransition } from "react";
import { useRouter } from "next/navigation";
import type { Entitlements } from "@/lib/api";

function setCookie(name: string, value: string) {
  // 30-day, path=/ so every portfolio page sees it; lax is fine (same-site owner tool).
  document.cookie = `${name}=${encodeURIComponent(value)}; path=/; max-age=2592000; samesite=lax`;
}

export function TierSimulator({ entitlements }: { entitlements: Entitlements }) {
  const router = useRouter();
  const [pending, start] = useTransition();

  // Owner-only: the server only emits simulator=true when METRON_TIER_SIMULATOR is on.
  if (!entitlements.simulator) return null;

  const onTier = (tier: string) => {
    setCookie("metron_preview_tier", tier);
    start(() => router.refresh());
  };
  const onFeed = (feed: boolean) => {
    setCookie("metron_preview_feed", String(feed));
    start(() => router.refresh());
  };

  return (
    <div
      role="region"
      aria-label="Tier simulator"
      className="mt-2 flex flex-wrap items-center gap-3 rounded-md border border-dashed border-line px-3 py-2 text-[12px] text-muted"
    >
      <span className="uppercase tracking-[0.14em]">Tier preview</span>
      <label className="flex items-center gap-1.5">
        <span className="sr-only">Preview tier</span>
        <select
          aria-label="Preview tier"
          value={entitlements.tier}
          disabled={pending}
          onChange={(e) => onTier(e.target.value)}
          className="rounded border border-line bg-surface px-2 py-1 text-ink disabled:opacity-50"
        >
          {entitlements.tiers.map((t) => (
            <option key={t.key} value={t.key}>
              {t.label}
            </option>
          ))}
        </select>
      </label>
      <label className="flex items-center gap-1.5">
        <input
          type="checkbox"
          aria-label="Market-data feed"
          checked={entitlements.feed_enabled}
          disabled={pending}
          onChange={(e) => onFeed(e.target.checked)}
        />
        Market-data feed
      </label>
      <span className="text-muted/60">owner-only preview — not shown on the public product</span>
    </div>
  );
}
