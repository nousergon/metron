// Live-session panel (metron-ops#153) — the Holdings LIVE valuation mode's session view:
// the NAV-weighted coverage banner, the covered-basis Overnight/Intraday/Day strip
// (relocated from the Overview, metron-ops#154), and the excluded-holdings disclosure.
//
// Covered-basis by construction (metron-ops#152): every $ and % here is computed over ONLY
// the holdings with a usable live quote — an unquoted holding is in neither the numerator
// nor the denominator, and is NAMED below with its reason instead of silently valued flat.
// A quoted-but-flat holding stays in (a real 0% move is information, not a coverage gap).
//
// Market closed → the intraday snapshot is stale and these figures are the COMPLETED
// session's closing state, labeled "as of close" — the honest last-session recap.

import type { IntradayStatus, IntradayLegHistory, Today } from "@/lib/api";
import { accountingMoneyWhole, moneyWhole, percent, signClass } from "@/lib/format";
import { StatCard } from "@/components/ui";

const EXCLUDED_REASON: Record<string, string> = {
  suspect: "quote failed the outlier guard",
  no_quote: "no live quote",
  no_fx: "no FX rate to base currency",
};

/** "11:03 AM" local, from the snapshot's UTC write time. */
function asOf(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
}

function CoverageBanner({ status, today, ccy }: { status: IntradayStatus; today: Today; ccy: string }) {
  const covered = status.covered_nav;
  const total = status.total_nav;
  const pct = covered != null && total ? covered / total : null;
  const when = asOf(status.as_of_utc ?? today.as_of_utc);
  const state = today.stale ? "session closed — as of close" : "~15-min delayed";
  return (
    <div className="rounded-md border border-line bg-surface px-3 py-2 text-xs text-muted">
      {covered != null && total != null ? (
        <>
          Live session covers{" "}
          <span className="font-medium text-ink">{moneyWhole(covered, ccy)}</span> of{" "}
          <span className="font-medium text-ink">{moneyWhole(total, ccy)}</span> NAV
          {pct != null ? <> ({percent(pct)})</> : null}
        </>
      ) : (
        <>Live session · coverage {today.n_priced}/{today.n_priced + today.n_excluded} holdings</>
      )}
      {" · "}
      {state}
      {when ? <> · as of {when}</> : null}
    </div>
  );
}

/** The covered-basis Overnight/Intraday/Day strip — percentages over covered prior-close
 *  MV only (`covered_prev_mv`), never the whole portfolio. */
function SessionStrip({ today, ccy }: { today: Today; ccy: string }) {
  return (
    <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-3">
      <StatCard
        label="Overnight"
        value={today.overnight_gain != null ? accountingMoneyWhole(today.overnight_gain, ccy) : "—"}
        valueClass={signClass(today.overnight_gain ?? 0)}
        hint={today.overnight_pct != null ? percent(today.overnight_pct) : undefined}
      />
      <StatCard
        label="Intraday"
        value={today.intraday_gain != null ? accountingMoneyWhole(today.intraday_gain, ccy) : "—"}
        valueClass={signClass(today.intraday_gain ?? 0)}
        hint={today.intraday_pct != null ? percent(today.intraday_pct) : undefined}
      />
      <StatCard
        label="Day"
        value={today.day_gain != null ? accountingMoneyWhole(today.day_gain, ccy) : "—"}
        valueClass={signClass(today.day_gain ?? 0)}
        hint={today.day_pct != null ? percent(today.day_pct) : undefined}
      />
    </div>
  );
}

export function SessionPanel({
  status,
  today,
  legs,
  ccy,
}: {
  status: IntradayStatus;
  today: Today;
  legs: IntradayLegHistory | null;
  ccy: string;
}) {
  if (!today.available || today.rows.length === 0) return null;
  const showLegs = (legs?.n_days ?? 0) > 0 && legs?.cum_day_pct != null;
  return (
    <section className="mt-4">
      <CoverageBanner status={status} today={today} ccy={ccy} />
      <SessionStrip today={today} ccy={ccy} />
      {today.covered_prev_mv != null ? (
        <p className="mt-1 text-[11px] text-muted/70">
          session %s over the covered basis — {moneyWhole(today.covered_prev_mv, ccy)} of prior-close market value
        </p>
      ) : null}
      {today.excluded_rows.length > 0 ? (
        <p className="mt-2 text-xs text-muted">
          Not in the live session ({today.excluded_rows.length}):{" "}
          {today.excluded_rows.map((e, i) => (
            <span key={e.ticker}>
              {i > 0 ? ", " : ""}
              <span className="text-ink/80" title={EXCLUDED_REASON[e.reason] ?? e.reason}>
                {e.label}
              </span>{" "}
              <span className="text-muted/70">({EXCLUDED_REASON[e.reason] ?? e.reason})</span>
            </span>
          ))}
          {" — "}valued at last close; in neither the session $ nor the session %.
        </p>
      ) : null}
      {showLegs && legs ? (
        <p className="mt-2 text-xs text-muted">
          Since tracking ({legs.n_days} day{legs.n_days === 1 ? "" : "s"}), cumulative drift split:{" "}
          <span className={signClass(legs.cum_overnight_pct ?? 0)}>
            overnight {legs.cum_overnight_pct != null ? percent(legs.cum_overnight_pct) : "—"}
          </span>{" "}
          ·{" "}
          <span className={signClass(legs.cum_intraday_pct ?? 0)}>
            intraday {legs.cum_intraday_pct != null ? percent(legs.cum_intraday_pct) : "—"}
          </span>{" "}
          ·{" "}
          <span className={signClass(legs.cum_day_pct ?? 0)}>
            day {legs.cum_day_pct != null ? percent(legs.cum_day_pct) : "—"}
          </span>
        </p>
      ) : null}
    </section>
  );
}
