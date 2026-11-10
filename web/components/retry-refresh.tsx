"use client";

// Background retry for a streamed section that failed (Brian, 2026-11-09: slow parts should
// "just materialize when they are finished loading"). Re-renders the page in place with
// router.refresh() — what is already on screen stays until the new render lands — a few
// times with growing gaps, then stops so a real outage does not loop forever.

import { useEffect } from "react";
import { useRouter } from "next/navigation";

export const RETRY_DELAYS_MS = [5_000, 15_000, 45_000];

function readAttempts(key: string): number {
  try {
    return Number(window.sessionStorage.getItem(key) ?? "0") || 0;
  } catch {
    return 0;
  }
}

function writeAttempts(key: string, n: number): void {
  try {
    window.sessionStorage.setItem(key, String(n));
  } catch {
    // storage unavailable: the in-memory schedule below still applies once
  }
}

export function RetryRefresh({ retryKey }: { retryKey: string }) {
  const router = useRouter();
  useEffect(() => {
    const key = `metron-retry:${retryKey}`;
    const n = readAttempts(key);
    if (n >= RETRY_DELAYS_MS.length) return;
    const timer = setTimeout(() => {
      writeAttempts(key, n + 1);
      router.refresh();
    }, RETRY_DELAYS_MS[n]);
    return () => clearTimeout(timer);
  }, [router, retryKey]);
  return null;
}

/** Clears a section's retry budget once it has rendered successfully. */
export function RetryReset({ retryKey }: { retryKey: string }) {
  useEffect(() => {
    try {
      window.sessionStorage.removeItem(`metron-retry:${retryKey}`);
    } catch {
      // nothing to clear
    }
  }, [retryKey]);
  return null;
}
