import type { ReactNode } from "react";

import { Empty, Locked } from "@/components/ui";
import { PortfolioNav } from "@/components/portfolio-nav";
import { GlanceScreen } from "@/components/glance-screen";
import { MetronApiError } from "@/lib/api";
import { getGlance } from "@/lib/api-glance";
import { featureEntitlement, loadEntitlements, previewFromCookies, toFeatureStates } from "@/lib/entitlements";
import { requireApiAuth } from "@/lib/session";

export const dynamic = "force-dynamic";

// The glance screen (metron-ops#248 Stage A): the whole screen from ONE aggregate fetch
// (metron-ops#250). Whole-portfolio scope; the analytics pages remain the depth layer.
export default async function GlancePage(props: { params: Promise<{ id: string }> }) {
  const { id } = await props.params;
  const apiAuth = await requireApiAuth();
  const entitlements = await loadEntitlements(apiAuth);
  const featureStates = toFeatureStates(entitlements);
  const ent = featureEntitlement(entitlements, "glance");

  const shell = (body: ReactNode) => (
    <div className="mx-auto w-full max-w-md lg:max-w-2xl">
      <PortfolioNav portfolioId={id} navQuery="" featureStates={featureStates} />
      {body}
    </div>
  );

  if (ent && !ent.available) {
    return shell(<Locked label="Glance" reason={ent.reason} requiredTier={ent.required_tier} />);
  }

  let g;
  try {
    g = await getGlance(apiAuth, id, previewFromCookies());
  } catch (e) {
    if (e instanceof MetronApiError && e.status === 404) return shell(<Empty>Portfolio not found.</Empty>);
    return shell(<Empty>Couldn&apos;t load the glance screen. Is the backend reachable?</Empty>);
  }
  return shell(<GlanceScreen g={g} portfolioId={id} />);
}
