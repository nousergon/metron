import { getMacro } from "@/lib/api";
import { Empty } from "@/components/ui";
import { PortfolioNav } from "@/components/portfolio-nav";
import { MacroChart } from "@/components/macro-chart";
import { isoDate } from "@/lib/format";
import { navFeatureStates } from "@/lib/entitlements";
import { requireTenantId } from "@/lib/session";
import Link from "next/link";

export const dynamic = "force-dynamic";

// Macro detail page (metron-ops) — reinstated from the #64 redirect. Key US macro
// indicators from FRED (public-domain → beta-safe, ungated) with a ~12-month chart each.
// Reached by clicking a tile in the Overview macro strip (anchored to the indicator).
export default async function MacroPage({ params }: { params: { id: string } }) {
  const { id } = params;
  const tenantId = await requireTenantId();
  const featureStates = await navFeatureStates(tenantId);

  let macro;
  try {
    macro = await getMacro(tenantId, { full: true });
  } catch {
    return <Empty>Couldn&apos;t load macro data. Is the backend running?</Empty>;
  }

  const ready = macro.available && macro.indicators.length > 0;

  return (
    <div>
      <PortfolioNav portfolioId={id} navQuery="" featureStates={featureStates} />

      <div className="mt-3 flex items-baseline justify-between gap-2">
        <h1 className="text-lg font-semibold">Macro</h1>
        <Link href={`/portfolios/${id}`} className="text-sm text-accent hover:underline">
          ← Overview
        </Link>
      </div>
      <p className="text-sm text-muted">
        Key US macro indicators from FRED (public-domain), ~12-month history.
        {macro.as_of ? ` Latest reading ${isoDate(macro.as_of)}.` : ""} Consensus forecasts and next-release
        dates aren&apos;t wired yet (metron-ops#49).
      </p>

      {ready ? (
        <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-2">
          {macro.indicators.map((ind) => (
            <MacroChart key={ind.key} ind={ind} />
          ))}
        </div>
      ) : (
        <div className="mt-4">
          <Empty>{macro.reason ?? "No macro data available yet."}</Empty>
        </div>
      )}
    </div>
  );
}
