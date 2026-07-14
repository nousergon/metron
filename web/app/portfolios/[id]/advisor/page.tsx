import { redirect } from "next/navigation";

export default async function AdvisorRedirect(props: { params: Promise<{ id: string }> }) {
  const params = await props.params;
  // Permanent redirect from /advisor → /intelligence (metron-ops#165)
  redirect(`/portfolios/${params.id}/intelligence`);
}
