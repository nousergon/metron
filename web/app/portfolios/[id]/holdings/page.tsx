import { redirect } from "next/navigation";

export const dynamic = "force-dynamic";

// Holdings IS the portfolio landing page now (metron-ops-I156) — this route redirects to
// the base, preserving the full query (account selection, ?val= regime, ?combine=), so
// every pre-move deep link and bookmark keeps working. Mirrors the today/ redirect.
export default async function HoldingsRedirect(
  props: {
    params: Promise<{ id: string }>;
    searchParams: Promise<Record<string, string | string[]>>;
  }
) {
  const searchParams = await props.searchParams;
  const params = await props.params;
  const qs = new URLSearchParams();
  for (const [key, value] of Object.entries(searchParams)) {
    for (const v of Array.isArray(value) ? value : [value]) qs.append(key, v);
  }
  const s = qs.toString();
  redirect(`/portfolios/${params.id}${s ? `?${s}` : ""}`);
}
