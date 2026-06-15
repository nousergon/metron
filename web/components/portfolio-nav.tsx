"use client";

// Portfolio navigation — one compact bar shared by every portfolio page: a back
// link, the portfolio name, and a "Pages" dropdown replacing the old long inline
// link row. The dropdown carries the current account selection (`navQuery`) onto
// every link so the panel filter follows across pages.

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";

export type NavPage = { label: string; href: string; feature?: string };
export type NavFeatureState = { available: boolean; required_tier: string | null };

// Short upsell labels for the lock badge (required_tier key → display).
const TIER_LABEL: Record<string, string> = { pro: "Pro", agentic: "Research+", personal: "Base" };

export function PortfolioNav({
  portfolioId,
  name,
  navQuery,
  plugins = [],
  featureStates,
}: {
  portfolioId: string;
  /** Portfolio display name — shown when the page has it fetched (the overview). */
  name?: string;
  /** `?account_id=…` selection string to carry across pages ("" = none). */
  navQuery: string;
  /** Premium plugin pages (metron-ops) appended to the menu; [] on the public tier. */
  plugins?: { id: string; label: string; href: string }[];
  /** feature key → availability (GET /meta/entitlements). Undefined = ungated (all shown). */
  featureStates?: Record<string, NavFeatureState>;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const pathname = usePathname();

  // Close on outside click / Escape.
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

  const base = `/portfolios/${portfolioId}`;
  // Selection-scoped pages carry navQuery; whole-portfolio pages don't.
  const pages: NavPage[] = [
    { label: "Overview", href: `${base}${navQuery}`, feature: "overview" },
    { label: "Performance", href: `${base}/performance${navQuery}`, feature: "performance" },
    { label: "Risk", href: `${base}/risk${navQuery}`, feature: "risk" },
    { label: "Attribution", href: `${base}/attribution${navQuery}`, feature: "attribution" },
    { label: "Transactions & realized", href: `${base}/transactions${navQuery}`, feature: "transactions" },
    { label: "Tax", href: `${base}/tax${navQuery}`, feature: "tax" },
    { label: "Macro", href: `${base}/macro`, feature: "macro" },
    { label: "Calendar", href: `${base}/calendar` },
    ...plugins.map((p) => ({ label: p.label, href: `${base}/${p.href}` })),
    { label: "Settings & data", href: `${base}/settings` },
  ];
  const current =
    pages.find((p) => p.href.split("?")[0] === pathname) ??
    (pathname === base ? pages[0] : undefined);

  return (
    <div className="flex items-center justify-between gap-4">
      <div className="flex min-w-0 items-baseline gap-3">
        <Link href="/" className="shrink-0 text-sm text-muted transition hover:text-ink">
          ← Portfolios
        </Link>
        {name ? <span className="truncate text-sm font-medium">{name}</span> : null}
      </div>
      <div ref={ref} className="relative shrink-0">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-haspopup="menu"
          aria-expanded={open}
          className="flex items-center gap-2 rounded-md border border-line px-3 py-1.5 text-[12px] uppercase tracking-[0.14em] text-muted transition hover:bg-white/5 hover:text-ink"
        >
          {current?.label ?? "Pages"}
          <svg viewBox="0 0 20 20" fill="currentColor" className={`h-3.5 w-3.5 transition ${open ? "rotate-180" : ""}`} aria-hidden="true">
            <path
              fillRule="evenodd"
              d="M5.22 8.22a.75.75 0 0 1 1.06 0L10 11.94l3.72-3.72a.75.75 0 1 1 1.06 1.06l-4.25 4.25a.75.75 0 0 1-1.06 0L5.22 9.28a.75.75 0 0 1 0-1.06Z"
              clipRule="evenodd"
            />
          </svg>
        </button>
        {open ? (
          <nav
            role="menu"
            className="absolute right-0 z-20 mt-2 w-56 overflow-hidden rounded-md border border-line bg-surface py-1 shadow-xl shadow-black/40"
          >
            {pages.map((p) => {
              const active = p.href.split("?")[0] === pathname;
              const state = p.feature ? featureStates?.[p.feature] : undefined;
              if (state && !state.available) {
                // Locked: not in the active tier (or needs the market-data feed).
                // Rendered non-clickable with the upsell tier so the boundary is visible.
                return (
                  <div
                    key={p.label}
                    role="menuitem"
                    aria-disabled="true"
                    className="flex cursor-not-allowed items-center justify-between gap-2 px-4 py-2 text-sm text-muted/40"
                  >
                    <span>{p.label}</span>
                    <span className="flex items-center gap-1 text-[10px] uppercase tracking-wider text-muted/60">
                      <span aria-hidden="true">🔒</span>
                      {TIER_LABEL[state.required_tier ?? ""] ?? state.required_tier ?? ""}
                    </span>
                  </div>
                );
              }
              return (
                <Link
                  key={p.label}
                  href={p.href}
                  role="menuitem"
                  onClick={() => setOpen(false)}
                  className={`block px-4 py-2 text-sm transition hover:bg-white/5 ${
                    active ? "text-ink" : "text-muted hover:text-ink"
                  }`}
                >
                  {p.label}
                </Link>
              );
            })}
          </nav>
        ) : null}
      </div>
    </div>
  );
}
