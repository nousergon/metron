import Link from "next/link";
import { getAdvisorProfile, MetronApiError, type AdvisorProfile } from "@/lib/api";
import { Empty, Section } from "@/components/ui";
import { AdvisorProfileForm } from "@/components/advisor-profile-form";
import { requireTenantId } from "@/lib/session";

export const dynamic = "force-dynamic";

export default async function AdvisorProfilePage({ params }: { params: { id: string } }) {
  const { id } = params;
  const tenantId = await requireTenantId();

  let profile: AdvisorProfile;
  try {
    profile = await getAdvisorProfile(tenantId, id);
  } catch (e) {
    if (e instanceof MetronApiError && e.status === 404) {
      return <Empty>The Advisor isn&apos;t available for this portfolio.</Empty>;
    }
    return <Empty>Couldn&apos;t load the profile. Is the backend running?</Empty>;
  }

  return (
    <div>
      <Link href={`/portfolios/${id}/advisor`} className="text-sm text-muted hover:text-ink">
        ← Advisor
      </Link>
      <Section title="Investor profile" note="the targets the Advisor compares your portfolio against — all fields optional">
        <AdvisorProfileForm portfolioId={id} initial={profile} />
      </Section>
    </div>
  );
}
