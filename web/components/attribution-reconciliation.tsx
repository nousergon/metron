import type { AttributionReconciliation } from "@/lib/api";
import { percent, signClass } from "@/lib/format";
import { Section, Table } from "@/components/ui";

function ret(v: number | null): string {
  return v != null ? percent(v) : "—";
}

// The decomposition against what actually happened over the same window (metron-ops-I372).
// It uses today's holdings at today's weights against a sector-ETF benchmark, so it is
// not expected to tie exactly. The residual is shown next to it, never hidden.
export function ReconciliationSection({ rec, benchmark }: { rec: AttributionReconciliation; benchmark: string }) {
  const rows: [string, number | null, number | null][] = [
    ["Portfolio (observed TWR vs attributed holdings)", rec.observed_portfolio_return, rec.portfolio_residual],
    [`${benchmark} (observed vs sector-ETF composite)`, rec.observed_benchmark_return, rec.benchmark_residual],
    ["Active return", rec.observed_active_return, rec.active_residual],
  ];
  return (
    <Section title="Reconciliation" note="the decomposition against the observed returns over the same window">
      <p className="text-sm text-muted">
        This decomposition uses today&apos;s holdings at today&apos;s weights, held through the window, against a
        benchmark rebuilt from sector ETFs. It is a current-holdings analysis, not realized attribution. The residual
        is what it leaves unexplained: trading and weight drift during the window, cash, excluded holdings, and the
        sector-ETF approximation.
        {rec.available
          ? rec.within_tolerance
            ? " It ties to the observed active return."
            : ` It does not tie to the observed active return within ${(rec.tolerance * 10000).toFixed(0)} bps.`
          : ` Not fully reconciled: ${rec.reason ?? "an observed return is unavailable"}.`}
      </p>
      <Table head={["", "Observed", "Residual"]}>
        {rows.map(([label, observed, residual]) => (
          <tr key={label} className="border-b border-line last:border-0">
            <td className="px-4 py-2 font-medium">{label}</td>
            <td className="px-4 py-2 text-right tabular-nums">{ret(observed)}</td>
            <td className={`px-4 py-2 text-right tabular-nums ${residual != null ? signClass(residual) : ""}`}>
              {ret(residual)}
            </td>
          </tr>
        ))}
      </Table>
    </Section>
  );
}
