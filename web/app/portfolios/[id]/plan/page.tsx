import { Empty } from "@/components/ui";
import { PortfolioNav } from "@/components/portfolio-nav";
import { PlanTargetsForm } from "@/components/plan-targets-form";
import { CashToTargetsPanel } from "@/components/cash-to-targets-panel";
import { WhatIfPurchasePanel } from "@/components/whatif-purchase-panel";
import { getPlanTargets, MetronApiError } from "@/lib/api-planning";
import { navFeatureStates } from "@/lib/entitlements";
import { requireApiAuth } from "@/lib/session";

export const dynamic = "force-dynamic";

export default async function PlanPage(props: { params: Promise<{ id: string }> }) {
  const params = await props.params;
  const { id } = params;
  const apiAuth = await requireApiAuth();
  const featureStates = await navFeatureStates(apiAuth);

  let targets;
  try {
    targets = await getPlanTargets(apiAuth, id);
  } catch (e) {
    if (e instanceof MetronApiError && e.status === 404) {
      return <Empty>Portfolio not found.</Empty>;
    }
    return <Empty>Couldn&apos;t load your plan. Is the backend running?</Empty>;
  }

  return (
    <div>
      <PortfolioNav portfolioId={id} navQuery="" featureStates={featureStates} />

      <h1 className="mt-3 text-lg font-semibold">Plan</h1>
      <p className="text-sm text-muted">
        Arithmetic against the targets you set. Metron does not choose securities — you type the number, Metron does
        the math.
      </p>

      <PlanTargetsForm portfolioId={id} initial={targets} />
      <CashToTargetsPanel portfolioId={id} hasTargets={targets.targets.length > 0} />
      <WhatIfPurchasePanel portfolioId={id} />

      {/* goal card slot — metron-ops#316 */}
    </div>
  );
}
