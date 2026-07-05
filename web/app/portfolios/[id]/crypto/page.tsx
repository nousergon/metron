import { getCrypto, MetronApiError } from "@/lib/api";
import { Empty } from "@/components/ui";
import { PortfolioNav } from "@/components/portfolio-nav";
import { CryptoPanel } from "@/components/crypto-panel";
import { navFeatureStates } from "@/lib/entitlements";
import { requireTenantId } from "@/lib/session";

export const dynamic = "force-dynamic";

export default async function CryptoPage(props: { params: Promise<{ id: string }> }) {
  const params = await props.params;
  const { id } = params;
  const tenantId = await requireTenantId();
  const featureStates = await navFeatureStates(tenantId);

  let summary;
  try {
    summary = await getCrypto(tenantId, id);
  } catch (e) {
    if (e instanceof MetronApiError && e.status === 404) {
      return <Empty>Portfolio not found.</Empty>;
    }
    return <Empty>Couldn&apos;t load crypto. Is the backend running?</Empty>;
  }

  return (
    <div>
      <PortfolioNav portfolioId={id} navQuery="" featureStates={featureStates} />

      <h1 className="mt-3 text-lg font-semibold">Crypto</h1>
      <p className="text-sm text-muted">
        Track BTC &amp; ETH by wallet address — or a BTC <span className="font-mono">xpub/ypub/zpub</span> to sum a
        whole HD wallet. Balances and USD value sync automatically. Standalone for now (separate from your
        holdings &amp; NAV) since crypto trades 24/7. Read-only: we never hold your keys (an xpub is watch-only).
      </p>

      <CryptoPanel portfolioId={id} summary={summary} />
    </div>
  );
}
