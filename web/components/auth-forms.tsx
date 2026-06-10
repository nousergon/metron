"use client";

import { useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { signIn, signUp } from "@/lib/auth-client";

const inputClass = "w-full rounded border border-line px-3 py-2 text-sm";
const buttonClass =
  "w-full rounded bg-ink px-3 py-2 text-sm font-medium text-white disabled:opacity-50";

function AuthShell({ title, children, alt }: { title: string; children: React.ReactNode; alt: React.ReactNode }) {
  return (
    <div className="mx-auto mt-12 max-w-sm">
      <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
      <div className="mt-4 space-y-3">{children}</div>
      <p className="mt-4 text-sm text-muted">{alt}</p>
    </div>
  );
}

export function SignInForm() {
  const router = useRouter();
  const [pending, start] = useTransition();
  const [error, setError] = useState<string | null>(null);

  function onSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const fd = new FormData(e.currentTarget);
    setError(null);
    start(async () => {
      const { error } = await signIn.email({
        email: String(fd.get("email")),
        password: String(fd.get("password")),
      });
      if (error) setError(error.message ?? "Sign-in failed.");
      else router.push("/");
    });
  }

  return (
    <AuthShell
      title="Sign in"
      alt={
        <>
          New here?{" "}
          <Link href="/signup" className="font-medium text-ink underline">
            Create an account
          </Link>
        </>
      }
    >
      <form onSubmit={onSubmit} className="space-y-3">
        <input className={inputClass} type="email" name="email" placeholder="Email" required autoComplete="email" />
        <input
          className={inputClass}
          type="password"
          name="password"
          placeholder="Password"
          required
          autoComplete="current-password"
        />
        {error ? <p className="text-sm text-negative">{error}</p> : null}
        <button className={buttonClass} disabled={pending} type="submit">
          {pending ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </AuthShell>
  );
}

export function SignUpForm() {
  const router = useRouter();
  const [pending, start] = useTransition();
  const [error, setError] = useState<string | null>(null);

  function onSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const fd = new FormData(e.currentTarget);
    setError(null);
    start(async () => {
      const { error } = await signUp.email({
        name: String(fd.get("name") || fd.get("email")),
        email: String(fd.get("email")),
        password: String(fd.get("password")),
      });
      if (error) setError(error.message ?? "Sign-up failed.");
      else router.push("/");
    });
  }

  return (
    <AuthShell
      title="Create your workspace"
      alt={
        <>
          Already have an account?{" "}
          <Link href="/login" className="font-medium text-ink underline">
            Sign in
          </Link>
        </>
      }
    >
      <form onSubmit={onSubmit} className="space-y-3">
        <input className={inputClass} type="text" name="name" placeholder="Name (optional)" autoComplete="name" />
        <input className={inputClass} type="email" name="email" placeholder="Email" required autoComplete="email" />
        <input
          className={inputClass}
          type="password"
          name="password"
          placeholder="Password (min 8 characters)"
          required
          minLength={8}
          autoComplete="new-password"
        />
        {error ? <p className="text-sm text-negative">{error}</p> : null}
        <button className={buttonClass} disabled={pending} type="submit">
          {pending ? "Creating…" : "Create account"}
        </button>
      </form>
    </AuthShell>
  );
}
