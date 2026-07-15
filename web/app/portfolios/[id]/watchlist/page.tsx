import { getWatchlist, MetronApiError } from "@/lib/api";
import { Empty } from "@/components/ui";
import { PortfolioNav } from "@/components/portfolio-nav";
import { WatchlistPanel } from "@/components/watchlist-panel";
import { navFeatureStates } from "@/lib/entitlements";
import { requireApiAuth } from "@/lib/session";

export const dynamic = "force-dynamic";

export default async function WatchlistPage(props: { params: Promise<{ id: string }> }) {
  const params = await props.params;
  const { id } = params;
  const apiAuth = await requireApiAuth();
  const featureStates = await navFeatureStates(apiAuth);

  let entries;
  try {
    entries = await getWatchlist(apiAuth, id);
  } catch (e) {
    if (e instanceof MetronApiError && e.status === 404) {
      return <Empty>Portfolio not found.</Empty>;
    }
    return <Empty>Couldn&apos;t load the watchlist. Is the backend running?</Empty>;
  }

  return (
    <div>
      <PortfolioNav portfolioId={id} navQuery="" featureStates={featureStates} />

      <h1 className="mt-3 text-lg font-semibold">Watchlist</h1>
      <p className="text-sm text-muted">
        Track tickers you don&apos;t hold — name, sector, and next earnings. Read-only in the beta: live prices
        arrive with the Pro market-data feed.
      </p>

      <WatchlistPanel portfolioId={id} entries={entries} />
    </div>
  );
}
