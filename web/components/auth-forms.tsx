"use client";

import { useState, useTransition } from "react";
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

// Passwordless sign-in: enter email → emailed a one-time link → click → in. The same
// flow for new and returning users (first link creates the workspace), so there's no
// separate signup screen.
//
// Private beta (metron-ops#18): new signups are invite-gated server-side (see
// lib/invite-gate.ts) — a brand-new email must submit a valid invite code, sent along
// as `metadata.inviteCode` on the magic-link request. Returning users (an account
// already exists for the email) are never gated, so the code field only matters the
// first time; showing it unconditionally keeps the form simple (the server can't tell
// the client "this email is new" without leaking whether an address is registered).
export function MagicLinkForm() {
  const [pending, start] = useTransition();
  const [error, setError] = useState<string | null>(null);
  const [sentTo, setSentTo] = useState<string | null>(null);

  function onSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    const email = String(form.get("email"));
    const inviteCode = String(form.get("inviteCode") ?? "").trim();
    setError(null);
    start(async () => {
      const { error } = await signIn.magicLink({
        email,
        callbackURL: "/",
        ...(inviteCode ? { metadata: { inviteCode } } : {}),
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
        <input
          className={inputClass}
          type="text"
          name="inviteCode"
          placeholder="Invite code (new accounts only)"
          autoComplete="off"
        />
        {error ? <p className="text-sm text-negative">{error}</p> : null}
        <button className={buttonClass} disabled={pending} type="submit">
          {pending ? "Sending…" : "Send sign-in link"}
        </button>
      </form>
    </AuthShell>
  );
}
