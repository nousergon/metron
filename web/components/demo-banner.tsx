import { cookies } from "next/headers";
import { getSession } from "@/lib/session";
import { DEMO_COOKIE } from "@/lib/demo";

// A thin banner shown only while browsing the read-only demo (the demo cookie is set and
// there's no real session). Makes the sample's read-only nature explicit and offers the
// sign-up path. (metron-ops#42)
export async function DemoBanner() {
  const inDemo = (await cookies()).get(DEMO_COOKIE)?.value === "1";
  if (!inDemo) return null;
  const session = await getSession();
  if (session?.user) return null; // a real session supersedes the demo cookie

  return (
    <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 border-b border-line bg-accent/10 px-4 py-2 text-xs">
      <span className="text-muted">
        <span className="font-medium text-ink">Demo</span> — read-only sample data. No signup or brokerage connection.
      </span>
      <div className="flex items-center gap-3">
        <a href="/login" className="font-medium text-accent hover:underline">
          Sign up to connect your accounts →
        </a>
        <form action="/demo" method="post">
          <button type="submit" className="text-muted hover:text-ink">
            Exit demo
          </button>
        </form>
      </div>
    </div>
  );
}
