import { getMarketBoard } from "@/lib/api-market-board";
import { MetronApiError } from "@/lib/api";
import { Empty } from "@/components/ui";
import { PortfolioNav } from "@/components/portfolio-nav";
import { MarketBoardPanel } from "@/components/market-board";
import { navFeatureStates } from "@/lib/entitlements";
import { requireApiAuth } from "@/lib/session";

export const dynamic = "force-dynamic";

export default async function MarketBoardPage(props: { params: Promise<{ id: string }> }) {
  const params = await props.params;
  const { id } = params;
  const apiAuth = await requireApiAuth();
  const featureStates = await navFeatureStates(apiAuth);

  let board;
  try {
    board = await getMarketBoard(apiAuth, id, "held");
  } catch (e) {
    if (e instanceof MetronApiError && e.status === 404) {
      // Either the portfolio doesn't exist, or (more commonly, since the nav already
      // hides this link) a no-feed beta build hit the route directly — the backend
      // collapses both to 404 (metron-ops-I304), so this shares one honest empty state.
      return (
        <div>
          <PortfolioNav portfolioId={id} navQuery="" featureStates={featureStates} />
          <Empty>This view isn&apos;t available.</Empty>
        </div>
      );
    }
    return (
      <div>
        <PortfolioNav portfolioId={id} navQuery="" featureStates={featureStates} />
        <Empty>Couldn&apos;t load the market board. Is the backend running?</Empty>
      </div>
    );
  }

  return (
    <div>
      <PortfolioNav portfolioId={id} navQuery="" featureStates={featureStates} />

      <h1 className="mt-3 text-lg font-semibold">Market</h1>
      <p className="text-sm text-muted">
        Which stocks are technically attractive today, across what you hold and watch.
      </p>

      <MarketBoardPanel portfolioId={id} initialScope="held" initialBoard={board} />
    </div>
  );
}
