"use client";

// "as of" timestamp in the VIEWER'S local time zone — the convention the Holdings header's
// live label (IntradayRefresher) already uses. A server component calling
// toLocaleTimeString formats in the SERVER's zone (UTC in deployment), so the session
// panel's coverage banner read "as of 3:20 PM" beside the header's "as of 8:20 AM" for the
// same snapshot on a Pacific-time phone (fix, 2026-09-28). Formatting after mount keeps
// the server and client renders identical (no hydration mismatch) — the label appears on
// hydration.

import { useEffect, useState } from "react";

/** "11:03 AM" local for a same-day snapshot; "Fri, Jul 18 · 4:00 PM" once the snapshot is
 *  from an earlier local calendar day (metron-ops-I156 supersession, 2026-07-22) — a bare
 *  time reads as "today" and would misrepresent a weekend/holiday view of the last completed
 *  session as fresher than it is. Pure; `now` is injectable for tests. */
export function asOfLabel(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const time = d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
  if (d.toDateString() === now.toDateString()) return time;
  const day = d.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" });
  return `${day} · ${time}`;
}

/** Renders `{prefix}{label}` in the viewer's local time once mounted; nothing before that
 *  or when the timestamp is missing/invalid. */
export function LocalAsOf({ iso, prefix = "" }: { iso: string | null | undefined; prefix?: string }) {
  const [label, setLabel] = useState("");
  useEffect(() => {
    setLabel(asOfLabel(iso));
  }, [iso]);
  if (!label) return null;
  return (
    <span>
      {prefix}
      {label}
    </span>
  );
}
