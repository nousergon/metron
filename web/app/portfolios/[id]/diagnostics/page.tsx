import { acctParams, getDiagnostics, MetronApiError } from "@/lib/api";
import { Empty, Locked } from "@/components/ui";
import { PortfolioNav } from "@/components/portfolio-nav";
import { AsOfClose } from "@/components/as-of-close";
import { TierSimulator } from "@/components/tier-simulator";
import { DiagnosticsCard } from "@/components/diagnostics-card";
import { featureEntitlement, loadEntitlements, toFeatureStates } from "@/lib/entitlements";
import { requireApiAuth } from "@/lib/session";
import { resolveAccountIds } from "@/lib/selection";

export const dynamic = "force-dynamic";

// Concentration & diversification diagnostics (metron-ops-I167) — the Intelligence
// lane's fully deterministic page (metron-ops-I164): portfolio-structure FACTS on the
// SETTLED context. Core analytics (free/beta `concentration` feature); only the
// benchmark-weight columns ride the licensed benchmark source and degrade honestly.
export default async function DiagnosticsPage({
  params,
  searchParams,
}: {
  params: { id: string };
  searchParams: { account_id?: string | string[] };
}) {
  const { id } = params;
  const apiAuth = await requireApiAuth();

  // URL selection wins; with none, the saved panel selection is applied (redirect).
  const accountIds = await resolveAccountIds(apiAuth, id, `/portfolios/${id}/diagnostics`, searchParams.account_id);
  const navQuery = acctParams(accountIds);

  // Entitlement gate — `concentration` is in every tier today, but a direct navigation
  // on a future packaging change still gets an honest Locked page, mirroring
  // Attribution. Owner-simulator preview honored via cookies.
  const entitlements = await loadEntitlements(apiAuth);
  const featureStates = toFeatureStates(entitlements);
  const ent = featureEntitlement(entitlements, "concentration");
  if (ent && !ent.available) {
    return (
      <div>
        <PortfolioNav portfolioId={id} navQuery={navQuery} featureStates={featureStates} />
        {entitlements ? <TierSimulator entitlements={entitlements} /> : null}
        <Locked label="Diagnostics" reason={ent.reason} requiredTier={ent.required_tier} />
      </div>
    );
  }

  let d;
  try {
    d = await getDiagnostics(apiAuth, id, accountIds);
  } catch (e) {
    if (e instanceof MetronApiError && e.status === 404) {
      return <Empty>Portfolio not found.</Empty>;
    }
    return <Empty>Couldn&apos;t load diagnostics. Is the backend running?</Empty>;
  }

  return (
    <div>
      <PortfolioNav portfolioId={id} navQuery={navQuery} featureStates={featureStates} />
      {entitlements ? <TierSimulator entitlements={entitlements} /> : null}

      <div className="mt-3 flex items-baseline gap-2">
        <h1 className="text-lg font-semibold">Diagnostics</h1>
        {/* SETTLED page (metron-ops#145/#146): as_of is the close date the valuation
            used, sourced from the API's own metadata — never static page copy. */}
        <AsOfClose date={d.as_of} />
      </div>
      <p className="text-sm text-muted">
        Portfolio structure, measured: concentration, sector weights vs {d.benchmark}, geography, and your own stated
        targets. Descriptive only — measurements, not recommendations.
        {accountIds.length > 0 ? " Scoped to the selected accounts." : ""}
      </p>

      {!d.computable ? (
        <div className="mt-4">
          <Empty>{d.reason ?? "Not computable yet."}</Empty>
        </div>
      ) : (
        <DiagnosticsCard d={d} />
      )}
    </div>
  );
}
