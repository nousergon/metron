import Link from "next/link";
import { getAlphaEngine, MetronApiError } from "@/lib/api";
import { money } from "@/lib/format";
import { Empty, Section, StatCard, Table } from "@/components/ui";
import { requireApiAuth } from "@/lib/session";

export const dynamic = "force-dynamic";

const DIR_CLASS: Record<string, string> = {
  UP: "text-positive",
  DOWN: "text-negative",
};

export default async function AlphaEnginePage({ params }: { params: { id: string } }) {
  const { id } = params;
  const apiAuth = await requireApiAuth();

  let view;
  try {
    view = await getAlphaEngine(apiAuth, id);
  } catch (e) {
    if (e instanceof MetronApiError && e.status === 404) {
      return <Empty>The Alpha Engine overlay isn&apos;t available for this portfolio.</Empty>;
    }
    return <Empty>Couldn&apos;t load the Alpha Engine view. Is the backend running?</Empty>;
  }

  return (
    <div>
      <div className="flex items-baseline justify-between">
        <Link href={`/portfolios/${id}`} className="text-sm text-muted hover:text-ink">
          ← Portfolio
        </Link>
        {view.as_of ? <span className="text-sm text-muted">signals as of {view.as_of}</span> : null}
      </div>

      {!view.available ? (
        <div className="mt-4">
          <Empty>Alpha Engine signals are unavailable: {view.reason}</Empty>
        </div>
      ) : (
        <>
          <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
            <StatCard label="Holdings" value={String(view.coverage.n_holdings)} />
            <StatCard label="Tracked" value={String(view.coverage.n_tracked)} hint="covered by research/predictor" />
            <StatCard label="EXIT signals" value={String(view.coverage.n_exit)} valueClass={view.coverage.n_exit > 0 ? "text-negative" : undefined} />
            <StatCard label="Momentum vetoes" value={String(view.coverage.n_veto)} valueClass={view.coverage.n_veto > 0 ? "text-negative" : undefined} />
          </div>

          <p className="mt-3 text-xs text-muted">
            For the stocks you hold, what the alpha-engine paper-trading system says — research signal + scores and the
            predictor&apos;s directional call. Informational only; not a recommendation to trade.
          </p>

          <Section title="Your holdings — system view" note="tracked holdings first">
            {view.holdings.length === 0 ? (
              <Empty>No holdings to match against signals.</Empty>
            ) : (
              <Table head={["Ticker", "Market value", "Signal", "Score", "Predicted", "Confidence", "Veto"]}>
                {view.holdings.map((h) => (
                  <tr key={h.ticker} className={`border-b border-line last:border-0 ${h.tracked ? "" : "opacity-50"}`}>
                    <td className="px-4 py-2 font-medium">{h.ticker}</td>
                    <td className="px-4 py-2 text-right tabular-nums">{h.market_value != null ? money(h.market_value) : "—"}</td>
                    <td className="px-4 py-2 text-right">{h.signal ?? "—"}</td>
                    <td className="px-4 py-2 text-right tabular-nums">{h.score != null ? h.score : "—"}</td>
                    <td className={`px-4 py-2 text-right ${h.predicted_direction ? DIR_CLASS[h.predicted_direction] ?? "" : ""}`}>
                      {h.predicted_direction ?? "—"}
                    </td>
                    <td className="px-4 py-2 text-right tabular-nums">
                      {h.prediction_confidence != null ? `${(h.prediction_confidence * 100).toFixed(0)}%` : "—"}
                    </td>
                    <td className="px-4 py-2 text-right">{h.momentum_veto ? "⚠︎" : ""}</td>
                  </tr>
                ))}
              </Table>
            )}
          </Section>

          {view.buy_candidates.length > 0 ? (
            <Section title="Buy candidates you don't hold" note="from the research scan">
              <Table head={["Ticker", "Score"]}>
                {view.buy_candidates.map((c) => (
                  <tr key={c.ticker} className="border-b border-line last:border-0">
                    <td className="px-4 py-2 font-medium">{c.ticker}</td>
                    <td className="px-4 py-2 text-right tabular-nums">{typeof c.score === "number" ? c.score : "—"}</td>
                  </tr>
                ))}
              </Table>
            </Section>
          ) : null}
        </>
      )}
    </div>
  );
}
