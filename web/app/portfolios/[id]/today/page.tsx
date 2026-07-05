import { redirect } from "next/navigation";
import { acctParams } from "@/lib/api";

export const dynamic = "force-dynamic";

// The standalone Today view was folded into Holdings (metron-ops#87): the intraday
// overnight/intraday/day decomposition now lives in the Holdings Day column (+ a
// day-summary strip), alongside per-security YTD/LTM. This route redirects, preserving
// any account selection, so old links/bookmarks keep working.
export default async function TodayRedirect(
  props: {
    params: Promise<{ id: string }>;
    searchParams: Promise<{ account_id?: string | string[] }>;
  }
) {
  const searchParams = await props.searchParams;
  const params = await props.params;
  const ids = searchParams.account_id;
  const list = ids == null ? [] : Array.isArray(ids) ? ids : [ids];
  redirect(`/portfolios/${params.id}/holdings${acctParams(list)}`);
}
