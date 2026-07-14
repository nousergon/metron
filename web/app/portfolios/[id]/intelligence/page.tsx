import Link from "next/link";
import { getAdvisor, MetronApiError } from "@/lib/api";
import { money } from "@/lib/format";
import { Empty, Section, StatCard, Table } from "@/components/ui";
import { GenerateAdvisor } from "@/components/generate-advisor";
import { requireApiAuth } from "@/lib/session";

export const dynamic = "force-dynamic";

const FLAG_LABEL: Record<string, string> = {
  avoid_violation: "avoid",
  overweight_pref: "preferred",
};

export default async function IntelligencePage({ params }: { params: { id: string } }) {
  const { id } = params;
  const apiAuth = await requireApiAuth();

  let view;
  try {
    view = await getAdvisor(apiAuth, id);
  } catch (e) {
    if (e instanceof MetronApiError && e.status === 404) {
      // Either the portfolio is missing or the Intelligence plugin isn't installed on this
      // deploy — both read as "not available here", never an error page.
      return <Empty>Intelligence isn&apos;t available for this portfolio.</Empty>;
    }
    return <Empty>Couldn&apos;t load Intelligence. Is the backend running?</Empty>;
  }

  const { analysis, commentary, has_profile } = view;
  const ccy = "USD";

  return (
    <div>
      <div className="flex items-baseline justify-between">
        <Link href={`/portfolios/${id}`} className="text-sm text-muted hover:text-ink">
          ← Portfolio
        </Link>
        <Link href={`/portfolios/${id}/intelligence/profile`} className="text-sm text-muted hover:text-ink">
          Edit profile →
        </Link>
      </div>

      <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
        <StatCard label="Value analyzed" value={money(analysis.nav, ccy)} hint={`${analysis.n_holdings} priced holdings`} />
        <StatCard label="Profile" value={has_profile ? "Set" : "Not set"} hint={has_profile ? "targets in effect" : "set targets to compare"} />
        <StatCard label="Concentration flags" value={String(analysis.concentration.length)} hint="over single-position cap" />
        <StatCard label="Income (est.)" value={analysis.income ? money(analysis.income.annual_income, ccy) : "—"} hint="annual dividends" />
      </div>

      <p className="mt-3 text-xs text-muted">
        Informational and educational only — not personalized investment advice. Figures are computed deterministically
        from your holdings; the narrative is an AI summary of those figures.
      </p>

      {analysis.nav <= 0 ? (
        <div className="mt-4">
          <Empty>No priced holdings yet — refresh prices on the portfolio page, then return here.</Empty>
        </div>
      ) : null}

      <Section title="Sector weights" note="share of analyzed value, by GICS sector">
        {analysis.sectors.length === 0 ? (
          <Empty>No sectors resolved yet.</Empty>
        ) : (
          <Table head={["Sector", "Weight", "Flag"]}>
            {analysis.sectors.map((s) => (
              <tr key={s.sector} className="border-b border-line last:border-0">
                <td className="px-4 py-2 font-medium">{s.sector}</td>
                <td className="px-4 py-2 text-right tabular-nums">{s.weight_pct.toFixed(1)}%</td>
                <td className="px-4 py-2 text-right text-muted">{FLAG_LABEL[s.flag] ?? ""}</td>
              </tr>
            ))}
          </Table>
        )}
      </Section>

      {analysis.concentration.length > 0 ? (
        <Section title="Concentration" note="positions over your single-position limit">
          <Table head={["Ticker", "Weight", "Limit"]}>
            {analysis.concentration.map((c) => (
              <tr key={c.ticker} className="border-b border-line last:border-0">
                <td className="px-4 py-2 font-medium">{c.ticker}</td>
                <td className="px-4 py-2 text-right tabular-nums text-negative">{c.weight_pct.toFixed(1)}%</td>
                <td className="px-4 py-2 text-right tabular-nums text-muted">{c.limit_pct.toFixed(1)}%</td>
              </tr>
            ))}
          </Table>
        </Section>
      ) : null}

      <Section
        title="AI commentary"
        note={
          commentary
            ? commentary.fresh
              ? `${commentary.model} · generated ${commentary.generated_at ?? ""}`
              : "portfolio changed since last run — regenerate for the current state"
            : "not generated yet"
        }
      >
        <div className="mb-3">
          <GenerateAdvisor portfolioId={id} label={commentary ? "Regenerate" : "Generate"} />
        </div>
        {commentary ? (
          <div className={commentary.fresh ? "" : "opacity-60"}>
            {commentary.narrative.split("\n").filter(Boolean).map((para, i) => (
              <p key={i} className="mb-3 text-sm leading-relaxed">
                {para}
              </p>
            ))}
            {commentary.considerations.length > 0 ? (
              <>
                <h3 className="mb-1 mt-4 text-sm font-semibold">Areas to consider</h3>
                <ul className="list-disc space-y-1 pl-5 text-sm">
                  {commentary.considerations.map((c, i) => (
                    <li key={i}>{c}</li>
                  ))}
                </ul>
              </>
            ) : null}
          </div>
        ) : (
          <Empty>No commentary yet. Click Generate to summarize the analysis above.</Empty>
        )}
      </Section>
    </div>
  );
}
