import { redirect } from "next/navigation";
import { acctParams } from "@/lib/api";

export const dynamic = "force-dynamic";

// The standalone Today view was folded into Holdings (metron-ops#87): the intraday
// overnight/intraday/day decomposition now lives in the Holdings Day column (+ a
// day-summary strip), alongside per-security YTD/LTM. This route redirects, preserving
// any account selection, so old links/bookmarks keep working.
export default function TodayRedirect({
  params,
  searchParams,
}: {
  params: { id: string };
  searchParams: { account_id?: string | string[] };
}) {
  const ids = searchParams.account_id;
  const list = ids == null ? [] : Array.isArray(ids) ? ids : [ids];
  redirect(`/portfolios/${params.id}${acctParams(list)}`);
}
