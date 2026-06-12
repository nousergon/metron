"use client";

import { useRouter } from "next/navigation";
import Link from "next/link";
import { signOut, useSession } from "@/lib/auth-client";

// Header session area: signed-in email + sign-out, or a sign-in link.
export function UserNav() {
  const router = useRouter();
  const { data, isPending } = useSession();

  if (isPending) return <span className="text-sm text-muted">…</span>;

  if (!data) {
    return (
      <Link href="/login" className="text-sm font-medium text-ink hover:underline">
        Sign in
      </Link>
    );
  }

  return (
    <div className="flex items-center gap-3 text-sm">
      <span className="text-muted">{data.user.email}</span>
      <button
        type="button"
        onClick={() => signOut().then(() => router.push("/login"))}
        className="rounded border border-line px-2 py-1 text-xs hover:bg-white/5"
      >
        Sign out
      </button>
    </div>
  );
}
