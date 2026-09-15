import { Empty } from "@/components/ui";
import { PortfolioNav } from "@/components/portfolio-nav";
import { LockedCards } from "@/components/locked-cards";
import { getLockedCards, type LockedCard } from "@/lib/api";
import { loadEntitlements, toFeatureStates } from "@/lib/entitlements";
import { requireApiAuth } from "@/lib/session";

export const dynamic = "force-dynamic";

// Features in development (metron-ops-I310): the locked cards an external-demo viewer
// sees for surfaces that exist but are not available to them. Copy comes from the API
// (api/services/external_demo.py), where the copy-lint test guards it.
export default async function InDevelopmentPage(props: { params: Promise<{ id: string }> }) {
  const { id } = await props.params;
  const apiAuth = await requireApiAuth();
  const featureStates = toFeatureStates(await loadEntitlements(apiAuth));

  let cards: LockedCard[] | null = null;
  try {
    cards = await getLockedCards(apiAuth);
  } catch {
    cards = null;
  }

  return (
    <div className="mx-auto w-full max-w-md lg:max-w-2xl">
      <PortfolioNav portfolioId={id} navQuery="" featureStates={featureStates} />
      <h1 className="mt-6 text-lg font-semibold tracking-tight">In development</h1>
      {cards === null ? <Empty>Couldn&apos;t load this page. Is the backend reachable?</Empty> : <LockedCards cards={cards} />}
    </div>
  );
}
