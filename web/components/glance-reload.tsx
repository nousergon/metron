"use client";

// Re-reads the glance payload. Deliberately named "Reload", not "Sync": it re-fetches what
// Metron already holds and never triggers a broker holdings refresh.

import { useTransition } from "react";
import { useRouter } from "next/navigation";

export function GlanceReload() {
  const router = useRouter();
  const [pending, start] = useTransition();
  return (
    <button
      type="button"
      onClick={() => start(() => router.refresh())}
      disabled={pending}
      className="rounded border border-line px-2 py-0.5 text-[11px] text-muted transition hover:text-ink disabled:opacity-50"
    >
      {pending ? "Reloading…" : "Reload"}
    </button>
  );
}
