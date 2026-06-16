import { getCalendar, MetronApiError } from "@/lib/api";
import { isoDate } from "@/lib/format";
import { Empty, Section, Table } from "@/components/ui";
import { PortfolioNav } from "@/components/portfolio-nav";
import { RefreshCalendar } from "@/components/refresh-calendar";
import { loadEntitlements } from "@/lib/entitlements";
import { requireTenantId } from "@/lib/session";

export const dynamic = "force-dynamic";

export default async function CalendarPage({ params }: { params: { id: string } }) {
  const { id } = params;
  const tenantId = await requireTenantId();

  let cal;
  try {
    cal = await getCalendar(tenantId, id);
  } catch (e) {
    if (e instanceof MetronApiError && e.status === 404) {
      return <Empty>Portfolio not found.</Empty>;
    }
    return <Empty>Couldn&apos;t load the calendar. Is the backend running?</Empty>;
  }

  const entitlements = await loadEntitlements(tenantId);

  return (
    <div>
      <PortfolioNav portfolioId={id} navQuery="" />

      <h1 className="mt-3 text-lg font-semibold">Calendar</h1>
      <p className="text-sm text-muted">
        Upcoming earnings for your holdings, within {cal.horizon_days} days. Refresh to re-source the dates.
      </p>

      <div className="mt-3">
        <RefreshCalendar portfolioId={id} feedOn={entitlements?.feed_enabled} />
      </div>

      <Section title="Upcoming earnings" note={`${cal.n_events} event${cal.n_events === 1 ? "" : "s"}`}>
        {cal.events.length === 0 ? (
          <Empty>No upcoming earnings cached — refresh to source them for your holdings.</Empty>
        ) : (
          <Table head={["Date", "Ticker", "Event"]}>
            {cal.events.map((e) => (
              <tr key={`${e.ticker}-${e.event_date}`} className="border-b border-line last:border-0">
                <td className="px-4 py-2 font-medium tabular-nums">{isoDate(e.event_date)}</td>
                <td className="px-4 py-2">{e.ticker}</td>
                <td className="px-4 py-2 text-muted">{e.label}</td>
              </tr>
            ))}
          </Table>
        )}
      </Section>
    </div>
  );
}
