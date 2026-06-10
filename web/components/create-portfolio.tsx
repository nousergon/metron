"use client";

import { useState, useTransition } from "react";
import { createPortfolioAction } from "@/app/actions";

// Create the first (or another) portfolio. On success the action redirects to the
// new portfolio; only the error case returns here.
export function CreatePortfolio() {
  const [pending, start] = useTransition();
  const [error, setError] = useState<string | null>(null);

  function onSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const fd = new FormData(e.currentTarget);
    setError(null);
    start(async () => {
      const res = await createPortfolioAction(fd);
      if (res && !res.ok) setError(res.message);
    });
  }

  return (
    <form onSubmit={onSubmit} className="flex flex-wrap items-center gap-2">
      <input
        type="text"
        name="name"
        placeholder="New portfolio name"
        required
        className="rounded border border-line px-3 py-2 text-sm"
      />
      <button
        type="submit"
        disabled={pending}
        className="rounded bg-ink px-3 py-2 text-sm font-medium text-white disabled:opacity-50"
      >
        {pending ? "Creating…" : "Create portfolio"}
      </button>
      {error ? <span className="text-sm text-negative">{error}</span> : null}
    </form>
  );
}
