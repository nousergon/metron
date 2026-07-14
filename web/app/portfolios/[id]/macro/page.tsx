import { getMacro, type MacroIndicator } from "@/lib/api";
import { Empty, Section, Table } from "@/components/ui";
import { PortfolioNav } from "@/components/portfolio-nav";
import { MacroChart } from "@/components/macro-chart";
import { isoDate, signClass } from "@/lib/format";
import { navFeatureStates } from "@/lib/entitlements";
import { requireApiAuth } from "@/lib/session";
import Link from "next/link";

export const dynamic = "force-dynamic";

function latest(ind: MacroIndicator): string {
  return ind.units === "%" ? `${ind.latest_value.toFixed(2)}%` : ind.latest_value.toFixed(2);
}

function delta(ind: MacroIndicator): string {
  if (ind.change == null) return "—";
  const sign = ind.change > 0 ? "+" : ind.change < 0 ? "−" : "";
  const mag = Math.abs(ind.change).toFixed(2);
  return ind.units === "%" ? `${sign}${mag} pp` : `${sign}${mag}`;
}

// Macro detail page (metron-ops) — reinstated from the #64 redirect. Key US macro
// indicators from FRED (public-domain → beta-safe, ungated) with a ~12-month chart each.
// Reached by clicking a tile in the Overview macro strip (anchored to the indicator).
export default async function MacroPage(props: { params: Promise<{ id: string }> }) {
  const params = await props.params;
  const { id } = params;
  const apiAuth = await requireApiAuth();
  const featureStates = await navFeatureStates(apiAuth);

  let macro;
  try {
    macro = await getMacro(apiAuth, { full: true });
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
        {macro.as_of ? ` Latest reading ${isoDate(macro.as_of)}.` : ""} The Next-expected column shows each
        series&apos; next scheduled release.
      </p>

      {ready ? (
        <>
          {/* Latest-readings table (metron-ops#49): freshness ("As of") + the
              producer-published next scheduled release ("Next expected", #13). */}
          <Section title="Latest readings">
            <Table head={["Indicator", "Latest", "Change", "As of", "Next expected"]}>
              {macro.indicators.map((ind) => (
                <tr key={ind.key} className="border-b border-line last:border-0">
                  <td className="px-4 py-2">
                    <Link href={`#${ind.key}`} className="hover:underline" title={`${ind.label} — chart`}>
                      {ind.label}
                    </Link>
                  </td>
                  <td className="px-4 py-2 text-right tabular-nums">{latest(ind)}</td>
                  <td className={`px-4 py-2 text-right tabular-nums ${signClass(ind.change ?? 0)}`}>{delta(ind)}</td>
                  <td className="px-4 py-2 text-right tabular-nums text-muted">{isoDate(ind.latest_date)}</td>
                  <td className="px-4 py-2 text-right tabular-nums text-muted">
                    {ind.next_release ? isoDate(ind.next_release) : "—"}
                  </td>
                </tr>
              ))}
            </Table>
          </Section>

          <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-2">
            {macro.indicators.map((ind) => (
              <MacroChart key={ind.key} ind={ind} />
            ))}
          </div>
        </>
      ) : (
        <div className="mt-4">
          <Empty>{macro.reason ?? "No macro data available yet."}</Empty>
        </div>
      )}
    </div>
  );
}
