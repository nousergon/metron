// Name-level benchmark-gap drivers (metron-ops-I346) — "you +1.8%, Nasdaq-100 +2.0%,
// because AppLovin (in the index, not held) beat and jumped 28.8%." Stage A only:
// deterministic ranked drivers + a factual `earnings` tag, never generated prose
// (ruling R5) — the waterfall chart and any narrative sentence are a later follow-up.
//
// Best-effort/supplemental: any fetch failure degrades to nothing rendered rather than
// breaking the Performance page the way a hard failure on the page's own data would.

import { BenchmarkGap, getBenchmarkGap, MetronApiError } from "@/lib/api";
import { percent, signClass } from "@/lib/format";
import { Empty, Section, Table } from "@/components/ui";

const CLASS_LABEL: Record<string, string> = {
  held: "Held, matched",
  not_held: "Not held",
  overweight: "Overweight",
  underweight: "Underweight",
};

export async function BenchmarkGapSection({
  apiAuth,
  portfolioId,
  index,
  accountIds,
}: {
  apiAuth: string;
  portfolioId: string;
  index: "SPX" | "NDX";
  accountIds?: string[];
}) {
  let gap: BenchmarkGap;
  try {
    gap = await getBenchmarkGap(apiAuth, portfolioId, index, accountIds);
  } catch (e) {
    if (e instanceof MetronApiError) return null; // supplemental — never break the page
    throw e;
  }

  const label = gap.index_label ?? index;

  if (!gap.computable) {
    return (
      <Section title={`Why vs ${label}`}>
        <Empty>{gap.reason ?? "Not computable yet."}</Empty>
      </Section>
    );
  }

  return (
    <Section
      title={`Why vs ${label}`}
      note={[
        gap.proxy_symbol ? `proxy ${gap.proxy_symbol}` : null,
        gap.weight_method ? `${gap.weight_method} weights` : null,
        gap.coverage_members != null ? `${gap.coverage_members} members` : null,
      ]
        .filter(Boolean)
        .join(" · ")}
    >
      <p className="text-sm text-muted">
        You {gap.portfolio_return != null ? percent(gap.portfolio_return) : "—"} vs {label}{" "}
        {gap.benchmark_return != null ? percent(gap.benchmark_return) : "—"}
        {gap.active_return != null
          ? ` (${percent(Math.abs(gap.active_return))} ${gap.active_return >= 0 ? "ahead" : "behind"})`
          : ""}
        . Reconciled — residual {gap.residual != null ? percent(gap.residual) : "—"}, tolerance{" "}
        {percent(gap.tolerance)}.
      </p>

      <div className="mt-3">
        <Table head={["Symbol", "Vs. index", "Port wt", "Index wt", "Return", "Active contribution", ""]}>
          {gap.drivers.map((d) => (
            <tr key={d.symbol} className="border-b border-line last:border-0">
              <td className="px-4 py-2 font-medium">{d.symbol}</td>
              <td className="px-4 py-2 text-muted">{CLASS_LABEL[d.classification] ?? d.classification}</td>
              <td className="px-4 py-2 text-right tabular-nums text-muted">{percent(d.port_weight)}</td>
              <td className="px-4 py-2 text-right tabular-nums text-muted">{percent(d.bench_weight)}</td>
              <td className="px-4 py-2 text-right tabular-nums">{percent(d.ret)}</td>
              <td className={`px-4 py-2 text-right tabular-nums ${signClass(d.active_contribution)}`}>
                {percent(d.active_contribution)}
              </td>
              <td className="px-4 py-2 text-right">
                {d.earnings ? (
                  <span className="whitespace-nowrap rounded border border-line px-1.5 py-0.5 text-[10px] uppercase tracking-wider text-muted">
                    earnings
                  </span>
                ) : null}
              </td>
            </tr>
          ))}
        </Table>
      </div>

      {gap.missing_returns.length > 0 ? (
        <p className="mt-2 text-xs text-muted">
          {gap.missing_returns.length} name{gap.missing_returns.length === 1 ? "" : "s"} had no return available and
          were excluded from the decomposition.
        </p>
      ) : null}
    </Section>
  );
}
