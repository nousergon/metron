import { getCalendar, MetronApiError } from "@/lib/api";
import { isoDate } from "@/lib/format";
import { Empty, Section, Table } from "@/components/ui";
import { PortfolioNav } from "@/components/portfolio-nav";
import { RefreshCalendar } from "@/components/refresh-calendar";
import { loadEntitlements, toFeatureStates } from "@/lib/entitlements";
import { requireApiAuth } from "@/lib/session";

export const dynamic = "force-dynamic";

/** Human label for an event kind (earnings | release | fomc — metron-ops#49). */
function eventType(kind: string): string {
  if (kind === "earnings") return "Earnings";
  if (kind === "fomc") return "FOMC";
  return "Macro";
}

export default async function CalendarPage(props: { params: Promise<{ id: string }> }) {
  const params = await props.params;
  const { id } = params;
  const apiAuth = await requireApiAuth();

  let cal;
  try {
    cal = await getCalendar(apiAuth, id);
  } catch (e) {
    if (e instanceof MetronApiError && e.status === 404) {
      return <Empty>Portfolio not found.</Empty>;
    }
    return <Empty>Couldn&apos;t load the calendar. Is the backend running?</Empty>;
  }

  const entitlements = await loadEntitlements(apiAuth);

  return (
    <div>
      <PortfolioNav portfolioId={id} navQuery="" featureStates={toFeatureStates(entitlements)} />

      <h1 className="mt-3 text-lg font-semibold">Calendar</h1>
      <p className="text-sm text-muted">
        Upcoming holding earnings plus macro events (FOMC + key releases), within {cal.horizon_days} days. Refresh
        to re-source the earnings dates.
      </p>

      <div className="mt-3">
        <RefreshCalendar portfolioId={id} feedOn={entitlements?.feed_enabled} />
      </div>

      <Section title="Upcoming events" note={`${cal.n_events} event${cal.n_events === 1 ? "" : "s"}`}>
        {cal.events.length === 0 ? (
          <Empty>No upcoming events — refresh to source earnings for your holdings.</Empty>
        ) : (
          <Table head={["Date", "Type", "Event"]}>
            {cal.events.map((e) => (
              <tr key={`${e.kind}-${e.ticker}-${e.event_date}`} className="border-b border-line last:border-0">
                <td className="px-4 py-2 font-medium tabular-nums">{isoDate(e.event_date)}</td>
                <td className="px-4 py-2">
                  <span className="rounded bg-white/5 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-muted">
                    {eventType(e.kind)}
                  </span>
                </td>
                <td className="px-4 py-2">{e.label}</td>
              </tr>
            ))}
          </Table>
        )}
      </Section>
    </div>
  );
}
