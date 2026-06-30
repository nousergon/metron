import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";
import { UserNav } from "@/components/user-nav";
import { DemoBanner } from "@/components/demo-banner";

export const metadata: Metadata = {
  title: "Metron — portfolio analytics, measured",
  description:
    "Multi-tenant portfolio analytics. No AI, no ads, no advice, read-only. True returns, attribution, income, tax clarity.",
  icons: {
    icon: [
      { url: "/favicon-32.png", sizes: "32x32", type: "image/png" },
      { url: "/favicon-192.png", sizes: "192x192", type: "image/png" },
      { url: "/favicon-512.png", sizes: "512x512", type: "image/png" },
    ],
    apple: { url: "/favicon-192.png", sizes: "192x192", type: "image/png" },
  },
};

// Apply the saved theme before first paint so light mode doesn't flash dark on load.
// Default (no key, or "dark") leaves <html> classless → the :root dark tokens apply.
const NO_FLASH_THEME = `try{if(localStorage.getItem("metron-theme")==="light")document.documentElement.classList.add("light")}catch(e){}`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <head>
        <script dangerouslySetInnerHTML={{ __html: NO_FLASH_THEME }} />
      </head>
      <body>
        <DemoBanner />
        <div className="mx-auto max-w-7xl px-6">
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
          <footer className="border-t border-line py-6 text-xs leading-relaxed text-muted">
            Read-only analytics. No ads, no trackers, no investment advice — we compute the numbers; what you do
            with them is up to you.
          </footer>
        </div>
      </body>
    </html>
  );
}
