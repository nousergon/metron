"use client";

// Accounts scope chip (metron-ops-I156) — account selection as a compact toolbar
// control instead of a full panel above the grid. "Accounts (n/N) ▾" opens a popover
// of checkbox rows (grouped by tax treatment, each with its market value) driving the
// SAME `?account_id=` machinery the old panel used (lib/use-account-selection). The
// grid is the page's hero; scoping is a control, not content. Account MANAGEMENT
// (delete/restore, full per-account metrics) lives on the Overview's Accounts panel.

import { useEffect, useMemo, useRef, useState } from "react";
import type { Account } from "@/lib/api";
import { moneyWhole } from "@/lib/format";
import { useAccountSelection } from "@/lib/use-account-selection";

// Tax-treatment grouping — mirrors the AccountPanel's grouping so the two surfaces
// read consistently. One group → no subheads.
const TAX_GROUP_ORDER = ["Taxable", "Tax-deferred", "Tax-exempt", "Other"];

function typeLabel(a: Account): string {
  if (a.tax_treatment === "taxable") return "Taxable";
  if (a.tax_treatment === "tax_deferred") return "Tax-deferred";
  if (a.tax_treatment === "tax_exempt") return "Tax-exempt";
  return a.taxable ? "Taxable" : "Other";
}

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
          role="menu"
          className="absolute left-0 z-20 mt-2 w-72 overflow-hidden rounded-md border border-line bg-surface py-1 shadow-xl shadow-black/40"
        >
          <button
            type="button"
            role="menuitemcheckbox"
            aria-checked={viewingAll}
            onClick={() => push([])}
            className="flex w-full items-center justify-between px-3 py-1.5 text-sm text-muted transition hover:bg-white/5 hover:text-ink"
          >
            <span>All accounts</span>
            {viewingAll ? <span aria-hidden="true">✓</span> : null}
          </button>
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
      ) : null}
    </div>
  );
}
