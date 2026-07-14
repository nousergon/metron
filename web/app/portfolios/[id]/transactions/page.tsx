import { redirect } from "next/navigation";

export const dynamic = "force-dynamic";

// Activity (realized lots + transactions) was bundled into the Tax page (metron-ops#66).
// This route is kept as a redirect so old links/bookmarks still resolve; the account
// selection is carried across.
export default async function TransactionsRedirect(
  props: {
    params: Promise<{ id: string }>;
    searchParams: Promise<{ account_id?: string | string[] }>;
  }
) {
  const searchParams = await props.searchParams;
  const params = await props.params;
  const sel = searchParams.account_id;
  const ids = sel == null ? [] : Array.isArray(sel) ? sel : [sel];
  const qs = new URLSearchParams();
  ids.forEach((v) => qs.append("account_id", v));
  const s = qs.toString();
  redirect(`/portfolios/${params.id}/tax${s ? `?${s}` : ""}`);
}
