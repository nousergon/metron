"use client";

// Accounts scope chip (metron-ops-I156) — account selection as a compact toolbar
// control instead of a full panel above the grid. "Accounts (n/N) ▾" opens a popover
// of checkbox rows (grouped by tax treatment, each with its market value) driving the
// SAME `?account_id=` machinery the old panel used (lib/use-account-selection). The
// grid is the page's hero; scoping is a control, not content. Account MANAGEMENT
// (delete/restore, full per-account metrics) lives on the Overview's Accounts panel.

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { Account } from "@/lib/api";
import { moneyWhole } from "@/lib/format";
import { useAccountSelection } from "@/lib/use-account-selection";

// Tax-treatment grouping — mirrors the AccountPanel's grouping so the two surfaces
// read consistently. One group → no subheads. "Tax-advantaged" (not "Other") for the
// taxable=false / no-tax_treatment bucket — matches AccountPanel's typeLabel() fallback
// and the Overview's "Tax-advantaged unrealized" tiles (metron-ops-I190-adjacent fix;
// the two components' fallback labels had drifted despite a comment claiming parity).
const TAX_GROUP_ORDER = ["Taxable", "Tax-deferred", "Tax-exempt", "Tax-advantaged"];

function typeLabel(a: Account): string {
  if (a.tax_treatment === "taxable") return "Taxable";
  if (a.tax_treatment === "tax_deferred") return "Tax-deferred";
  if (a.tax_treatment === "tax_exempt") return "Tax-exempt";
  return a.taxable ? "Taxable" : "Tax-advantaged";
}

// Popover sizing/positioning constants. VIEWPORT_MARGIN keeps a breathing gap off the
// browser chrome; MAX_VH caps how much of the viewport the panel may ever claim (the
// rest scrolls internally instead of pushing off-screen — metron-ops bug: on short
// viewports the un-bounded panel extended past window bottom with no way to reach the
// last group, most visibly "Tax-advantaged" since it sorts last).
const VIEWPORT_MARGIN = 12;
const MAX_VH_RATIO = 0.6;
const MIN_PANEL_HEIGHT = 120;

function accountLabel(a: Account): string {
  return a.nickname || a.name || a.external_id || a.account_id;
}

export function AccountScopeChip({
  accounts,
  baseCurrency,
  portfolioId,
}: {
  accounts: Account[];
  baseCurrency: string;
  portfolioId?: string;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const headerRef = useRef<HTMLButtonElement>(null);
  // Hand-rolled viewport-collision handling (no popover-positioning primitive is in use
  // elsewhere in this codebase yet — see package.json check before adding one). "down" is
  // the default open direction; flips to "up" when there isn't room below the trigger.
  // maxHeightPx is recomputed alongside placement so the panel never claims more of the
  // viewport than is actually available in the chosen direction — it scrolls internally
  // (overflow-y-auto below) instead of extending past window bounds.
  const [placement, setPlacement] = useState<"down" | "up">("down");
  const [maxHeightPx, setMaxHeightPx] = useState<number | null>(null);
  const allIds = useMemo(() => accounts.map((a) => a.account_id), [accounts]);
  const { selected, viewingAll, navPending, push, toggle } = useAccountSelection(portfolioId, allIds);

  // Close on outside click / Escape (the portfolio-nav popover pattern).
  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  // Measure the trigger against the viewport whenever the panel opens (and on resize
  // while open) and pick the placement/max-height that keeps it fully on-screen.
  // useLayoutEffect so this resolves before paint — the panel renders "down" on the
  // very first frame and never visibly flashes/flips.
  useLayoutEffect(() => {
    if (!open) return;
    const recompute = () => {
      const trigger = buttonRef.current;
      if (!trigger) return;
      // The "All accounts" header row is a fixed (non-scrolling) part of the panel —
      // its measured height is subtracted from the available space so the header +
      // scrollable region together never exceed what actually fits (not just the
      // scrollable region alone).
      const headerHeight = headerRef.current?.getBoundingClientRect().height ?? 0;
      const triggerRect = trigger.getBoundingClientRect();
      const spaceBelow = window.innerHeight - triggerRect.bottom - VIEWPORT_MARGIN;
      const spaceAbove = triggerRect.top - VIEWPORT_MARGIN;
      const preferredMax = window.innerHeight * MAX_VH_RATIO - headerHeight;
      const openUp = spaceBelow < MIN_PANEL_HEIGHT && spaceAbove > spaceBelow;
      const available = (openUp ? spaceAbove : spaceBelow) - headerHeight;
      setPlacement(openUp ? "up" : "down");
      setMaxHeightPx(Math.max(MIN_PANEL_HEIGHT - headerHeight, Math.min(preferredMax, available)));
    };
    recompute();
    window.addEventListener("resize", recompute);
    window.addEventListener("scroll", recompute, true);
    return () => {
      window.removeEventListener("resize", recompute);
      window.removeEventListener("scroll", recompute, true);
    };
  }, [open]);

  const groups = useMemo(() => {
    const map = new Map<string, Account[]>();
    for (const a of accounts) {
      const key = typeLabel(a);
      const bucket = map.get(key);
      if (bucket) bucket.push(a);
      else map.set(key, [a]);
    }
    return [...map.entries()].sort((x, y) => {
      const ix = TAX_GROUP_ORDER.indexOf(x[0]);
      const iy = TAX_GROUP_ORDER.indexOf(y[0]);
      return (ix === -1 ? 99 : ix) - (iy === -1 ? 99 : iy) || x[0].localeCompare(y[0]);
    });
  }, [accounts]);

  if (accounts.length <= 1) return null; // nothing to scope

  return (
    <div ref={ref} className="relative">
      <button
        ref={buttonRef}
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="menu"
        aria-expanded={open}
        className="inline-flex items-center gap-1.5 rounded-lg border border-line px-2.5 py-1 text-xs text-muted transition hover:text-ink"
        title="Scope the page to a subset of accounts"
      >
        Accounts ({viewingAll ? "all" : `${selected.size}/${accounts.length}`})
        {navPending ? <span className="text-[10px] text-muted/70">…</span> : null}
        <svg viewBox="0 0 20 20" fill="currentColor" className={`h-3 w-3 transition ${open ? "rotate-180" : ""}`} aria-hidden="true">
          <path
            fillRule="evenodd"
            d="M5.22 8.22a.75.75 0 0 1 1.06 0L10 11.94l3.72-3.72a.75.75 0 1 1 1.06 1.06l-4.25 4.25a.75.75 0 0 1-1.06 0L5.22 9.28a.75.75 0 0 1 0-1.06Z"
            clipRule="evenodd"
          />
        </svg>
      </button>
      {open ? (
        <div
          ref={panelRef}
          role="menu"
          // Anchors below the trigger by default; flips to open upward (bottom-anchored)
          // when there isn't room below (see useLayoutEffect above). Internal content
          // order ("All accounts" then groups) stays the same either way — only the
          // anchor point moves. z-30 (not z-20): the holdings table's sticky <thead> is
          // also z-20 and renders later in the DOM, so at a tied z-index it painted on
          // top and visually clipped the bottom of this panel (metron-ops, Brian
          // 2026-07-20 report) — the panel must outrank the sticky header, not tie it.
          className={`absolute left-0 z-30 flex w-72 flex-col overflow-hidden rounded-md border border-line bg-surface shadow-xl shadow-black/40 ${
            placement === "up" ? "bottom-full mb-2" : "top-full mt-2"
          }`}
        >
          <button
            ref={headerRef}
            type="button"
            role="menuitemcheckbox"
            aria-checked={viewingAll}
            onClick={() => push([])}
            className="flex w-full shrink-0 items-center justify-between border-b border-line px-3 py-1.5 text-sm text-muted transition hover:bg-white/5 hover:text-ink"
          >
            <span>All accounts</span>
            {viewingAll ? <span aria-hidden="true">✓</span> : null}
          </button>
          {/* Scrollable region — bounded to the space actually available in the chosen
              direction (see useLayoutEffect above) so long account lists scroll WITHIN
              the popover instead of extending past the viewport (metron-ops bug fix). */}
          <div className="overflow-y-auto py-1" style={maxHeightPx != null ? { maxHeight: maxHeightPx } : undefined}>
            {groups.map(([label, accts]) => (
              <div key={label}>
                {groups.length > 1 ? (
                  <div className="px-3 pb-0.5 pt-2 text-[10px] uppercase tracking-wide text-muted/70">{label}</div>
                ) : null}
                {accts.map((a) => (
                  <label
                    key={a.account_id}
                    className="flex cursor-pointer items-center gap-2 px-3 py-1.5 text-sm transition hover:bg-white/5"
                  >
                    <input
                      type="checkbox"
                      checked={selected.has(a.account_id)}
                      onChange={() => toggle(a.account_id)}
                      className="h-3.5 w-3.5 accent-current"
                    />
                    <span className="min-w-0 flex-1 truncate text-ink/90">{accountLabel(a)}</span>
                    <span className="shrink-0 text-xs tabular-nums text-muted">
                      {a.market_value != null ? moneyWhole(a.market_value, baseCurrency) : "—"}
                    </span>
                  </label>
                ))}
              </div>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}
