import { redirect } from "next/navigation";

export default function AdvisorRedirect({ params }: { params: { id: string } }) {
  // Permanent redirect from /advisor → /intelligence (metron-ops#165)
  redirect(`/portfolios/${params.id}/intelligence`);
}
