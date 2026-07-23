"use client";

import { useState, useEffect } from "react";
import { useRouter, usePathname } from "next/navigation";
import Link from "next/link";
import { signOut, useSession } from "@/lib/auth-client";

export function MobileNav() {
  const [open, setOpen] = useState(false);
  const pathname = usePathname();
  const router = useRouter();
  const { data, isPending } = useSession();

  // Close nav on route change (link click).
  useEffect(() => {
    setOpen(false);
  }, [pathname]);

  // Prevent body scroll when the drawer is open.
  useEffect(() => {
    document.body.style.overflow = open ? "hidden" : "";
    return () => { document.body.style.overflow = ""; };
  }, [open]);

  return (
    <>
      {/* Hamburger — visible below lg */}
      <button
        type="button"
        className="flex items-center justify-center lg:hidden"
        onClick={() => setOpen((v) => !v)}
        aria-label={open ? "Close menu" : "Open menu"}
      >
        <svg
          className="h-6 w-6"
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          strokeWidth={2}
        >
          {open ? (
            <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
          ) : (
            <path strokeLinecap="round" strokeLinejoin="round" d="M4 6h16M4 12h16M4 18h16" />
          )}
        </svg>
      </button>

      {/* Overlay backdrop */}
      {open && (
        <div
          className="fixed inset-0 z-40 bg-black/40 lg:hidden"
          onClick={() => setOpen(false)}
        />
      )}

      {/* Slide-out drawer */}
      <div
        className={`fixed inset-y-0 right-0 z-50 w-72 border-l border-line bg-bg px-6 py-6 shadow-xl transition-transform duration-200 lg:hidden ${
          open ? "translate-x-0" : "translate-x-full"
        }`}
      >
        <div className="flex items-center justify-between">
          <span className="text-base font-semibold uppercase tracking-[0.22em]">Metron</span>
          <button
            type="button"
            onClick={() => setOpen(false)}
            className="text-muted hover:text-fg"
            aria-label="Close menu"
          >
            <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        <div className="mt-8 space-y-4">
          {isPending ? (
            <p className="text-sm text-muted">…</p>
          ) : data ? (
            <>
              <p className="truncate text-sm text-muted">{data.user.email}</p>
              <button
                type="button"
                onClick={() => signOut().then(() => router.push("/login"))}
                className="block w-full rounded border border-line px-3 py-2 text-left text-sm hover:bg-white/5"
              >
                Sign out
              </button>
            </>
          ) : (
            <Link
              href="/login"
              className="block rounded border border-line px-3 py-2 text-sm font-medium hover:bg-white/5"
              onClick={() => setOpen(false)}
            >
              Sign in
            </Link>
          )}
        </div>
      </div>
    </>
  );
}
