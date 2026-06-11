import Link from "next/link";
import { getRisk, MetronApiError } from "@/lib/api";
import { percent } from "@/lib/format";
import { Empty, Section, StatCard, Table } from "@/components/ui";
import { ComputeRisk } from "@/components/compute-risk";
import { requireTenantId } from "@/lib/session";

export const dynamic = "force-dynamic";

function vol(v: number | null): string {
  return v != null ? `${(v * 100).toFixed(1)}%` : "—";
}

export default async function RiskPage({ params }: { params: { id: string } }) {
  const { id } = params;
  const tenantId = await requireTenantId();

  let risk;
  try {
    risk = await getRisk(tenantId, id);
  } catch (e) {
    if (e instanceof MetronApiError && e.status === 404) {
      return <Empty>Portfolio not found.</Empty>;
    }
    return <Empty>Couldn&apos;t load risk. Is the backend running?</Empty>;
  }

  const factors = Object.keys(risk.factor_exposures);

  return (
    <div>
      <Link href={`/portfolios/${id}`} className="text-sm text-muted hover:text-ink">
        ← Portfolio
      </Link>

      <h1 className="mt-3 text-lg font-semibold">Factor risk</h1>
      <p className="text-sm text-muted">
        Ex-ante volatility decomposed into market + style factors and idiosyncratic risk, with tracking error vs{" "}
        {risk.benchmark}. Annualized, from daily returns.
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
