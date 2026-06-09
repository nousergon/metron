import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

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
          <header className="flex items-baseline justify-between border-b border-line py-5">
            <Link href="/" className="text-lg font-semibold tracking-tight">
              Metron
            </Link>
            <span className="text-sm text-muted">portfolio analytics, measured</span>
          </header>
          <main className="py-8">{children}</main>
          <footer className="border-t border-line py-6 text-xs text-muted">
            No AI · no ads or trackers · no advice · read-only. We compute; we never tell you what to trade.
          </footer>
        </div>
      </body>
    </html>
  );
}
