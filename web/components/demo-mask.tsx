"use client";

// Mounts the demo currency mask (metron-ops-I305 deliverable 2) for the whole app.
//
// Off unless asked for: `?demo_mask=1` turns it on and persists it to a session cookie
// so it survives the navigation steps of the recorded demo path; `?demo_mask=0` turns it
// off again. With neither, this component renders nothing and observes nothing, so the
// ordinary app carries no mask cost.
//
// While on, a MutationObserver re-masks anything that arrives after first paint (SWR
// revalidation, a lazily-mounted panel, a chart tooltip). `data-demo-mask-applied` goes
// on <html> after the first pass — the recorder BLOCKS on that attribute before it films
// anything, so a frame of unmasked balances cannot reach the video.

import { useEffect } from "react";
import { DEMO_MASK_APPLIED_ATTR, DEMO_MASK_COOKIE, maskFromCookie, maskFromSearch } from "@/lib/demo-mask";
import { applyMask } from "@/lib/demo-mask-dom";

function persist(on: boolean): void {
  // Session cookie (no Max-Age): the mask is a recording-time flag, not a preference,
  // and it must not outlive the browser that asked for it.
  document.cookie = on
    ? `${DEMO_MASK_COOKIE}=1; path=/; SameSite=Lax`
    : `${DEMO_MASK_COOKIE}=; path=/; Max-Age=0; SameSite=Lax`;
}

export function DemoMask() {
  useEffect(() => {
    const fromQuery = maskFromSearch(window.location.search);
    if (fromQuery !== null) persist(fromQuery);
    const on = fromQuery ?? maskFromCookie(document.cookie);

    if (!on) {
      document.documentElement.removeAttribute(DEMO_MASK_APPLIED_ATTR);
      return;
    }

    let base = 0;
    const run = () => {
      base = applyMask(document.body, base).base;
      document.documentElement.setAttribute(DEMO_MASK_APPLIED_ATTR, "1");
    };

    run();

    const observer = new MutationObserver((records) => {
      // Our own rewrites are mutations too; re-running is safe because a masked string
      // no longer matches the currency pattern, so the second pass is a no-op and the
      // loop converges immediately.
      if (records.length === 0) return;
      run();
    });
    observer.observe(document.body, { childList: true, subtree: true, characterData: true, attributes: true });

    return () => {
      observer.disconnect();
      document.documentElement.removeAttribute(DEMO_MASK_APPLIED_ATTR);
    };
  }, []);

  return null;
}
