"use client";

import { useState, useTransition } from "react";
import Link from "next/link";
import { signIn } from "@/lib/auth-client";

const inputClass = "w-full rounded border border-line px-3 py-2 text-sm";
const buttonClass = "w-full rounded bg-ink px-3 py-2 text-sm font-medium text-paper hover:bg-white disabled:opacity-50";

function AuthShell({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="mx-auto mt-12 max-w-sm">
      <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
      <div className="mt-4 space-y-3">{children}</div>
    </div>
  );
}

// Passwordless sign-in against the SHARED nousergon-auth identity service
// (metron-ops#179): enter email → emailed a one-time link → click → in. The same flow
// for new and returning users (first link creates the account; the workspace is
// provisioned by Metron's backend on first authenticated request), so there's no
// separate signup screen. The shared service's verify endpoint sets the
// cross-subdomain session cookie AND redirects back here itself — Metron has no local
// /auth/verify page.
//
// Private beta (metron-ops#18, gate now centralized in nousergon-auth): new signups
// are allowlist-gated server-side — an admin pre-approves the email address itself
// (per product), so there's nothing extra for the user to type. `metadata.product`
// is REQUIRED (the shared gate rejects a new signup without it). Returning users
// (an account already exists for the email) are never gated.
export function MagicLinkForm() {
  const [pending, start] = useTransition();
  const [error, setError] = useState<string | null>(null);
  const [sentTo, setSentTo] = useState<string | null>(null);

  function onSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    const email = String(form.get("email"));
    setError(null);
    start(async () => {
      // callbackURL must be ABSOLUTE: the verify link lives on the shared auth
      // origin, so a bare "/" would land on auth.nousergon.ai, not Metron.
      const { error } = await signIn.magicLink({
        email,
        callbackURL: `${window.location.origin}/`,
        metadata: { product: "metron" },
      });
      if (error) setError(error.message ?? "Couldn't send the sign-in link.");
      else setSentTo(email);
    });
  }

  if (sentTo) {
    return (
      <AuthShell title="Check your email">
        <p className="text-sm text-muted">
          We sent a sign-in link to <span className="font-medium text-ink">{sentTo}</span>. Click it to continue — it
          expires shortly and can be used once.
        </p>
        <button type="button" onClick={() => setSentTo(null)} className="text-sm text-muted underline">
          Use a different email
        </button>
      </AuthShell>
    );
  }

  return (
    <AuthShell title="Sign in to Metron">
      <p className="text-sm text-muted">Enter your email and we&apos;ll send you a one-time sign-in link.</p>
      <form onSubmit={onSubmit} className="space-y-3">
        <input className={inputClass} type="email" name="email" placeholder="Email" required autoComplete="email" />
        {error ? <p className="text-sm text-negative">{error}</p> : null}
        <button className={buttonClass} disabled={pending} type="submit">
          {pending ? "Sending…" : "Send sign-in link"}
        </button>
        {/* Beta-tester acceptance (metron-ops#203): no separate signup screen exists
            (first magic-link click both authenticates and provisions the workspace —
            see the module docstring above), so this "by continuing" notice next to the
            single actionable control IS the consent gesture; the backend records its
            timestamp on `users.tos_accepted_at` at JIT-provision time. */}
        <p className="text-xs text-muted">
          By continuing you agree to our{" "}
          <Link href="/terms" className="underline hover:text-fg">
            Terms
          </Link>{" "}
          and{" "}
          <Link href="/privacy" className="underline hover:text-fg">
            Privacy Policy
          </Link>
          .
        </p>
      </form>
    </AuthShell>
  );
}
