import {
  getHoldings,
  getResearchIntel,
  MetronApiError,
  type ResearchIntelAttractiveness,
} from "@/lib/api";
import { Empty, Locked, Section, Table } from "@/components/ui";
import { PortfolioNav } from "@/components/portfolio-nav";
import { TierSimulator } from "@/components/tier-simulator";
import { featureEntitlement, loadEntitlements, toFeatureStates } from "@/lib/entitlements";
import { requireApiAuth } from "@/lib/session";
import { isoDate } from "@/lib/format";
import Link from "next/link";

export const dynamic = "force-dynamic";

// Read-only research-intel surface (EPIC config#1499 Phase 1 / metron-ops#117). Displays
// the neutral market intel — regime + narrative, sector ratings, and per-HOLDING
// attractiveness + generic thesis — sourced from the crucible-research `research_intel`
// artifact. Paid Intelligence tier only: the nav hides the link on the beta, and a direct
// navigation lands on a full-page <Locked>. NO advice, NO position directives, NO LLM —
// impersonal intelligence the user applies to their own portfolio (EPIC decision 7).

const RATING_LABEL: Record<string, string> = {
  overweight: "Overweight",
  market_weight: "Market weight",
  underweight: "Underweight",
};

function ratingClass(rating: string | null): string {
  if (rating === "overweight") return "text-positive";
  if (rating === "underweight") return "text-negative";
  return "text-muted";
}

function score(a: ResearchIntelAttractiveness): string {
  return a.score == null ? "—" : a.score.toFixed(0);
}

export default async function ResearchIntelPage({ params }: { params: { id: string } }) {
  const { id } = params;
  const apiAuth = await requireApiAuth();

  // Entitlement gate: research_intel is packaged to the paid Intelligence tier. The nav
  // locks the link off-tier, but a direct navigation reaches here → full-page Locked
  // (and we skip the data fetch). Owner tier-simulator preview honored via cookies.
  const entitlements = await loadEntitlements(apiAuth);
  const featureStates = toFeatureStates(entitlements);
  const ent = featureEntitlement(entitlements, "research_intel");
  if (ent && !ent.available) {
    return (
      <div>
        <PortfolioNav portfolioId={id} navQuery="" featureStates={featureStates} />
        {entitlements ? <TierSimulator entitlements={entitlements} /> : null}
        <Locked label="Research intel" reason={ent.reason} requiredTier={ent.required_tier} />
      </div>
    );
  }

  // Scope the attractiveness map to the user's holdings (falls back to the full universe
  // if holdings can't be read — the intel surface degrades, never blanks).
  let tickers: string[] = [];
  try {
    tickers = (await getHoldings(apiAuth, id)).map((h) => h.ticker).filter(Boolean);
  } catch {
    tickers = [];
  }

  let res;
  try {
    res = await getResearchIntel(apiAuth, { tickers });
  } catch (e) {
    if (e instanceof MetronApiError && e.status === 404) {
      return <Empty>Portfolio not found.</Empty>;
    }
    return <Empty>Couldn&apos;t load research intel. Is the backend running?</Empty>;
  }

  const header = (
    <>
      <PortfolioNav portfolioId={id} navQuery="" featureStates={featureStates} />
      <div className="mt-3 flex items-baseline justify-between gap-2">
        <h1 className="text-lg font-semibold">Research intel</h1>
        <Link href={`/portfolios/${id}`} className="text-sm text-accent hover:underline">
          ← Overview
        </Link>
      </div>
    </>
  );

  if (!res.intel) {
    return (
      <div>
        {header}
        <div className="mt-4">
          <Empty>
            {res.stale
              ? "The weekly research-intel run hasn't published an artifact yet — check back after the next run."
              : "No research intel available."}
          </Empty>
        </div>
      </div>
    );
  }

  const intel = res.intel;
  const sectors = Object.keys(intel.sector_ratings).sort();
  const heldAttractiveness = tickers
    .map((t) => intel.attractiveness[t] ?? intel.attractiveness[t.toUpperCase()])
    .filter((a): a is ResearchIntelAttractiveness => Boolean(a));
  // If holdings couldn't be resolved, show whatever the universe scoped to (never blank).
  const rows = heldAttractiveness.length > 0 ? heldAttractiveness : Object.values(intel.attractiveness);

  return (
    <div>
      {header}
      <p className="text-sm text-muted">
        Neutral market intelligence from the research engine
        {intel.date ? ` for ${isoDate(intel.date)}` : ""}. Impersonal analysis you apply to your own
        holdings — not personalized buy/sell advice.
      </p>

      <Section title="Market regime">
        <div className="px-4 py-3">
          <span className="text-base font-semibold capitalize">{intel.market_regime ?? "—"}</span>
          {intel.regime_narrative ? (
            <p className="mt-1 text-sm text-muted">{intel.regime_narrative}</p>
          ) : null}
          {intel.market_breadth.pct_above_50d_ma != null ? (
            <p className="mt-2 text-xs text-muted tabular-nums">
              Breadth: {intel.market_breadth.pct_above_50d_ma.toFixed(0)}% above 50d MA
              {intel.market_breadth.pct_above_200d_ma != null
                ? ` · ${intel.market_breadth.pct_above_200d_ma.toFixed(0)}% above 200d MA`
                : ""}
            </p>
          ) : null}
        </div>
      </Section>

      {sectors.length > 0 ? (
        <Section title="Sector ratings">
          <Table head={["Sector", "Rating", "Rationale"]}>
            {sectors.map((s) => {
              const r = intel.sector_ratings[s];
              return (
                <tr key={s} className="border-b border-line last:border-0">
                  <td className="px-4 py-2">{s}</td>
                  <td className={`px-4 py-2 ${ratingClass(r.rating)}`}>
                    {RATING_LABEL[r.rating ?? ""] ?? "—"}
                  </td>
                  <td className="px-4 py-2 text-sm text-muted">{r.rationale ?? "—"}</td>
                </tr>
              );
            })}
          </Table>
        </Section>
      ) : null}

      <Section
        title={heldAttractiveness.length > 0 ? "Your holdings — attractiveness" : "Attractiveness"}
      >
        {rows.length > 0 ? (
          <Table head={["Ticker", "Sector", "Attractiveness", "Thesis"]}>
            {rows.map((a) => (
              <tr key={a.ticker} className="border-b border-line last:border-0">
                <td className="px-4 py-2 font-medium">{a.ticker}</td>
                <td className="px-4 py-2 text-sm text-muted">{a.sector ?? "—"}</td>
                <td className="px-4 py-2 text-right tabular-nums">{score(a)}</td>
                <td className="px-4 py-2 text-sm text-muted">{a.thesis?.bull_case ?? "—"}</td>
              </tr>
            ))}
          </Table>
        ) : (
          <Empty>No attractiveness scores cover your current holdings yet.</Empty>
        )}
      </Section>
    </div>
  );
}
