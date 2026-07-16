"use client";

// Settled-page refresher (metron-ops#154). Overview and the Holdings settled mode value
// everything from the official EOD close, so there is nothing to poll intraday — but the
// once-daily NAV snapshot still advances after the close, and an all-day-open tab must
// pick that up without a manual reload (the "Today tile stuck on 6/29" bug, metron#119 /
// metron-ops#131). One slow router.refresh loop, no label, no status fetch — settled
// surfaces make no freshness claims beyond their as-of badges.

import { useEffect } from "react";
import { useRouter } from "next/navigation";

// TEMPORARY 3x throttle (30min → 90min) for the remainder of the July 2026 Neon billing
// cycle only — extra safety margin on top of the metron-ops#198 caching fix while headroom
// on the free-tier egress quota is thin. Revert to 30 * 60 * 1000 after the Aug 1 reset
// (tracked: metron-ops#199, calendar reminder set) once #198's real-world impact is confirmed.
const SLOW_MS = 90 * 60 * 1000;

export function SettledRefresher() {
  const router = useRouter();

  useEffect(() => {
    const timer = setInterval(() => router.refresh(), SLOW_MS);
    return () => clearInterval(timer);
  }, [router]);

  return null;
}
