import { acctParams, getAttribution, MetronApiError } from "@/lib/api";
import { percent, signClass } from "@/lib/format";
import { Empty, Locked, Section, StatCard, Table } from "@/components/ui";
import { PortfolioNav } from "@/components/portfolio-nav";
import { AsOfClose } from "@/components/as-of-close";
import { TierSimulator } from "@/components/tier-simulator";
import { ComputeAttribution } from "@/components/compute-attribution";
import { featureEntitlement, loadEntitlements, toFeatureStates } from "@/lib/entitlements";
import { requireApiAuth } from "@/lib/session";
import { resolveAccountIds } from "@/lib/selection";

export const dynamic = "force-dynamic";

function weight(v: number): string {
  return `${(v * 100).toFixed(1)}%`;
}

function ret(v: number | null): string {
  return v != null ? percent(v) : "—";
}

export default async function AttributionPage({
  params,
  searchParams,
}: {
  params: { id: string };
  searchParams: { account_id?: string | string[] };
}) {
  const { id } = params;
  const apiAuth = await requireApiAuth();

  // URL selection wins; with none, the saved panel selection is applied (redirect).
  const accountIds = await resolveAccountIds(apiAuth, id, `/portfolios/${id}/attribution`, searchParams.account_id);
  const navQuery = acctParams(accountIds);

  // Entitlement gate: Attribution is feed-dependent. The nav hides the link when
  // excluded, but a direct navigation reaches here — render a full-page Locked instead
  // of the page (and skip the data fetch). Owner-simulator preview honored via cookies.
  const entitlements = await loadEntitlements(apiAuth);
  const featureStates = toFeatureStates(entitlements);
  const attrEnt = featureEntitlement(entitlements, "attribution");
  if (attrEnt && !attrEnt.available) {
    return (
      <div>
        <PortfolioNav portfolioId={id} navQuery={navQuery} featureStates={featureStates} />
        {entitlements ? <TierSimulator entitlements={entitlements} /> : null}
        <Locked label="Sector attribution" reason={attrEnt.reason} requiredTier={attrEnt.required_tier} />
      </div>
    );
  }

  let attr;
  try {
    attr = await getAttribution(apiAuth, id, accountIds);
  } catch (e) {
    if (e instanceof MetronApiError && e.status === 404) {
      return <Empty>Portfolio not found.</Empty>;
    }
    return <Empty>Couldn&apos;t load attribution. Is the backend running?</Empty>;
  }

  return (
    <div>
      <PortfolioNav portfolioId={id} navQuery={navQuery} featureStates={featureStates} />
      {entitlements ? <TierSimulator entitlements={entitlements} /> : null}

      <div className="mt-3 flex items-baseline gap-2">
        <h1 className="text-lg font-semibold">Sector attribution</h1>
        {/* SETTLED tab (metron-ops#145/#146): as_of is the freshest close bar the window
            returns were computed from — the decomposition's true data horizon. */}
        <AsOfClose date={attr.as_of} />
      </div>
      <p className="text-sm text-muted">
        Brinson-Fachler decomposition of active return vs {attr.benchmark} into allocation (sector tilts), selection
        (picks within a sector), and interaction
        {attr.lookback_days ? `, over the trailing ${attr.lookback_days} days` : ""}.
        {accountIds.length > 0 ? " Scoped to the selected accounts." : ""}
      </p>

      <div className="mt-3">
        <ComputeAttribution portfolioId={id} />
      </div>

      {!attr.computable ? (
        <div className="mt-4">
          <Empty>
            {attr.reason ?? "Not computable yet."} The nightly refresh sources sectors + history automatically, or
            click “Compute attribution” to do it now.
          </Empty>
        </div>
      ) : (
        <>
          <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
            <StatCard
              label={`Active return vs ${attr.benchmark}`}
              value={attr.active_return != null ? percent(attr.active_return) : "—"}
              hint={`${attr.n_sectors} sectors, ${(attr.coverage * 100).toFixed(0)}% covered`}
            />
            <StatCard label="Allocation" value={attr.allocation != null ? percent(attr.allocation) : "—"} />
            <StatCard label="Selection" value={attr.selection != null ? percent(attr.selection) : "—"} />
            <StatCard label="Interaction" value={attr.interaction != null ? percent(attr.interaction) : "—"} />
          </div>

          <Section title="By sector" note="portfolio vs benchmark weight + return, and the decomposed effects">
            <Table
              head={["Sector", "Port wt", "Bench wt", "Port ret", "Bench ret", "Allocation", "Selection", "Interaction", "Total"]}
            >
              {attr.sectors.map((s) => (
                <tr key={s.sector} className="border-b border-line last:border-0">
                  <td className="px-4 py-2 font-medium">{s.sector}</td>
                  <td className="px-4 py-2 text-right tabular-nums text-muted">{weight(s.port_weight)}</td>
                  <td className="px-4 py-2 text-right tabular-nums text-muted">{weight(s.bench_weight)}</td>
                  <td className="px-4 py-2 text-right tabular-nums">{ret(s.port_return)}</td>
                  <td className="px-4 py-2 text-right tabular-nums text-muted">{ret(s.bench_return)}</td>
                  <td className={`px-4 py-2 text-right tabular-nums ${signClass(s.allocation)}`}>{percent(s.allocation)}</td>
                  <td className={`px-4 py-2 text-right tabular-nums ${signClass(s.selection)}`}>{percent(s.selection)}</td>
                  <td className={`px-4 py-2 text-right tabular-nums ${signClass(s.interaction)}`}>{percent(s.interaction)}</td>
                  <td className={`px-4 py-2 text-right font-medium tabular-nums ${signClass(s.total)}`}>{percent(s.total)}</td>
                </tr>
              ))}
            </Table>
          </Section>

          {attr.coverage < 1 ? (
            <Section title="Coverage" note="market value attributed to a GICS sector">
              <p className="text-sm text-muted">
                {(attr.coverage * 100).toFixed(0)}% of priced market value is sector-classified; the remainder is held in
                positions without a resolved sector (e.g. cash, funds, or an unclassifiable ticker) and is excluded from
                the decomposition rather than attributed to a guess.
              </p>
            </Section>
          ) : null}
        </>
      )}
    </div>
  );
}
