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
// session's closing state, labeled "as of close" — the honest last-session recap. This framing
// was written generically enough that it needed no change to also cover a fully "closed"
// session (pre-market/weekend/holiday, not just same-day post-close "recap") once page.tsx
// stopped clamping the regime to settled there (metron-ops-I156 supersession, 2026-07-22) —
// the panel simply mounts more often now, showing the last live snapshot frozen indefinitely
// instead of vanishing. Only the "as of" timestamp needed a fix (below): a bare time reads as
// "today" and would misrepresent a multi-day-old frozen snapshot as fresher than it is.
//
// Split in two (Brian, 2026-09-28): SessionPanel is the headline (coverage banner + strip);
// SessionNotes carries the explanatory footnotes (covered-basis denominator, the
// not-in-the-live-session list, the since-tracking drift split), which the landing page
// renders in its notes block at the BOTTOM of the page rather than between the headline
// cards and the holdings. The coverage figure is a SHARE, not a change, so it renders
// unsigned ("(91.3%)", never "(+91.3%)"), and its "as of" time renders client-side in the
// viewer's local zone (LocalAsOf) — the same convention as the page header's live label.

import type { IntradayStatus, IntradayLegHistory, Today } from "@/lib/api";
import { accountingMoneyWhole, moneyWhole, pct1, percent, signClass } from "@/lib/format";
import { StatCard } from "@/components/ui";
import { LocalAsOf } from "@/components/local-as-of";

const EXCLUDED_REASON: Record<string, string> = {
  suspect: "quote failed the outlier guard",
  no_quote: "no live quote",
  no_fx: "no FX rate to base currency",
};

function CoverageBanner({ status, today, ccy }: { status: IntradayStatus; today: Today; ccy: string }) {
  const covered = status.covered_nav;
  const total = status.total_nav;
  const pct = covered != null && total ? covered / total : null;
  const state = today.stale ? "session closed — as of close" : "~15-min delayed";
  return (
    <div className="rounded-md border border-line bg-surface px-3 py-2 text-xs text-muted">
      {covered != null && total != null ? (
        <>
          Live session covers{" "}
          <span className="font-medium text-ink">{moneyWhole(covered, ccy)}</span> of{" "}
          <span className="font-medium text-ink">{moneyWhole(total, ccy)}</span> NAV
          {pct != null ? <> ({pct1(pct)})</> : null}
        </>
      ) : (
        <>Live session · coverage {today.n_priced}/{today.n_priced + today.n_excluded} holdings</>
      )}
      {" · "}
      {state}
      <LocalAsOf iso={status.as_of_utc ?? today.as_of_utc} prefix=" · as of " />
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

/** The live session's headline: NAV-weighted coverage banner + the covered-basis strip. */
export function SessionPanel({
  status,
  today,
  ccy,
}: {
  status: IntradayStatus;
  today: Today;
  ccy: string;
}) {
  if (!today.available || today.rows.length === 0) return null;
  return (
    <section className="mt-4">
      <CoverageBanner status={status} today={today} ccy={ccy} />
      <SessionStrip today={today} ccy={ccy} />
    </section>
  );
}

/** The live session's explanatory footnotes — rendered in the landing page's bottom notes.
 *  Null whenever SessionPanel is (no session to annotate) or there is nothing to note. */
export function SessionNotes({
  today,
  legs,
  ccy,
}: {
  today: Today;
  legs: IntradayLegHistory | null;
  ccy: string;
}) {
  if (!today.available || today.rows.length === 0) return null;
  const showLegs = (legs?.n_days ?? 0) > 0 && legs?.cum_day_pct != null;
  if (today.covered_prev_mv == null && today.excluded_rows.length === 0 && !showLegs) return null;
  return (
    <div className="space-y-2">
      {today.covered_prev_mv != null ? (
        <p className="text-xs text-muted">
          Session %s over the covered basis — {moneyWhole(today.covered_prev_mv, ccy)} of prior-close market value.
        </p>
      ) : null}
      {today.excluded_rows.length > 0 ? (
        <p className="text-xs text-muted">
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
        <p className="text-xs text-muted">
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
    </div>
  );
}
