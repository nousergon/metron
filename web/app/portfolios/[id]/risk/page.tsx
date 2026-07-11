import { acctParams, getRisk, MetronApiError } from "@/lib/api";
import { percent } from "@/lib/format";
import { Empty, Locked, Section, StatCard, Table } from "@/components/ui";
import { PortfolioNav } from "@/components/portfolio-nav";
import { AsOfClose } from "@/components/as-of-close";
import { TierSimulator } from "@/components/tier-simulator";
import { ComputeRisk } from "@/components/compute-risk";
import { featureEntitlement, loadEntitlements, toFeatureStates } from "@/lib/entitlements";
import { requireApiAuth } from "@/lib/session";
import { resolveAccountIds } from "@/lib/selection";

export const dynamic = "force-dynamic";

function vol(v: number | null): string {
  return v != null ? `${(v * 100).toFixed(1)}%` : "—";
}

export default async function RiskPage({
  params,
  searchParams,
}: {
  params: { id: string };
  searchParams: { account_id?: string | string[] };
}) {
  const { id } = params;
  const apiAuth = await requireApiAuth();

  // URL selection wins; with none, the saved panel selection is applied (redirect).
  const accountIds = await resolveAccountIds(apiAuth, id, `/portfolios/${id}/risk`, searchParams.account_id);
  const navQuery = acctParams(accountIds);

  // Entitlement gate: Risk is feed-dependent. The nav hides the link when excluded,
  // but a direct navigation reaches here — render a full-page Locked instead of the
  // page (and skip the data fetch). The owner-simulator preview is honored via cookies.
  const entitlements = await loadEntitlements(apiAuth);
  const featureStates = toFeatureStates(entitlements);
  const riskEnt = featureEntitlement(entitlements, "risk");
  if (riskEnt && !riskEnt.available) {
    return (
      <div>
        <PortfolioNav portfolioId={id} navQuery={navQuery} featureStates={featureStates} />
        {entitlements ? <TierSimulator entitlements={entitlements} /> : null}
        <Locked label="Factor risk" reason={riskEnt.reason} requiredTier={riskEnt.required_tier} />
      </div>
    );
  }

  let risk;
  try {
    risk = await getRisk(apiAuth, id, accountIds);
  } catch (e) {
    if (e instanceof MetronApiError && e.status === 404) {
      return <Empty>Portfolio not found.</Empty>;
    }
    return <Empty>Couldn&apos;t load risk. Is the backend running?</Empty>;
  }

  const factors = Object.keys(risk.factor_exposures);

  return (
    <div>
      <PortfolioNav portfolioId={id} navQuery={navQuery} featureStates={featureStates} />
      {entitlements ? <TierSimulator entitlements={entitlements} /> : null}

      <div className="mt-3 flex items-baseline gap-2">
        <h1 className="text-lg font-semibold">Factor risk</h1>
        {/* SETTLED tab (metron-ops#145/#146): as_of is the model's aligned-grid end date —
            its true data horizon — not the compute-call date. */}
        <AsOfClose date={risk.as_of} />
      </div>
      <p className="text-sm text-muted">
        Ex-ante volatility decomposed into market + style factors and idiosyncratic risk, with tracking error vs{" "}
        {risk.benchmark}. Annualized, from daily returns.
        {accountIds.length > 0 ? " Scoped to the selected accounts." : ""}
      </p>

      <div className="mt-3">
        <ComputeRisk portfolioId={id} />
      </div>

      {!risk.computable ? (
        <div className="mt-4">
          <Empty>
            {risk.reason ?? "Not computable yet."} The nightly refresh backfills this automatically, or click
            “Compute risk” to do it now.
          </Empty>
        </div>
      ) : (
        <>
          <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
            <StatCard label="Total volatility" value={vol(risk.total_vol)} hint={`${risk.n_obs} days, ${risk.n_modeled} holdings`} />
            <StatCard label="Factor vol" value={vol(risk.factor_vol)} hint={risk.idio_pct != null ? `idio ${(risk.idio_pct * 100).toFixed(0)}%` : undefined} />
            <StatCard label="Idiosyncratic vol" value={vol(risk.idio_vol)} />
            <StatCard label={`Tracking error vs ${risk.benchmark}`} value={vol(risk.tracking_error)} />
          </div>

          <Section title="Factor exposures" note="net βᵀw + variance share">
            <Table head={["Factor", "Exposure", "Variance contribution"]}>
              {factors.map((f) => (
                <tr key={f} className="border-b border-line last:border-0">
                  <td className="px-4 py-2 font-medium">{f}</td>
                  <td className="px-4 py-2 text-right tabular-nums">{risk.factor_exposures[f].toFixed(2)}</td>
                  <td className="px-4 py-2 text-right tabular-nums text-muted">
                    {risk.factor_pct_contrib[f] != null ? percent(risk.factor_pct_contrib[f]) : "—"}
                  </td>
                </tr>
              ))}
            </Table>
          </Section>

          {risk.excluded.length > 0 ? (
            <Section title="Excluded holdings" note="too little aligned history to model">
              <p className="text-sm text-muted">{risk.excluded.join(", ")}</p>
            </Section>
          ) : null}
        </>
      )}
    </div>
  );
}
