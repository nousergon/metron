import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";
import { UserNav } from "@/components/user-nav";

export const metadata: Metadata = {
  title: "Metron — portfolio analytics, measured",
  description:
    "Multi-tenant portfolio analytics. No AI, no ads, no advice, read-only. True returns, attribution, income, tax clarity.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <div className="mx-auto max-w-6xl px-6">
          <header className="flex items-center justify-between border-b border-line py-5">
            <Link href="/" className="flex items-baseline gap-3">
              <span className="text-base font-semibold uppercase tracking-[0.22em]">Metron</span>
              <span className="hidden text-[11px] uppercase tracking-[0.18em] text-muted sm:inline">
                portfolio analytics, measured
              </span>
            </Link>
            <UserNav />
          </header>
          <main className="py-8">{children}</main>
          <footer className="border-t border-line py-6 text-[11px] uppercase tracking-[0.14em] text-muted">
            No AI · no ads or trackers · no advice · read-only. We compute; we never tell you what to trade.
          </footer>
        </div>
      </body>
    </html>
  );
}
