import Link from "next/link";
import { getPortfolios, MetronApiError, tenantConfigured, type Portfolio } from "@/lib/api";
import { Empty } from "@/components/ui";

export const dynamic = "force-dynamic";

export default async function HomePage() {
  if (!tenantConfigured()) {
    return (
      <Empty>
        Set <code className="rounded bg-slate-100 px-1">METRON_DEV_TENANT_ID</code> (a tenant UUID created via the
        API) and <code className="rounded bg-slate-100 px-1">METRON_API_URL</code> to view portfolios. Auth replaces
        this placeholder in PH4.
      </Empty>
    );
  }

  let portfolios: Portfolio[] = [];
  try {
    portfolios = await getPortfolios();
  } catch (e) {
    const detail = e instanceof MetronApiError ? `(${e.status})` : "";
    return <Empty>Couldn&apos;t reach the Metron API {detail}. Is the backend running at METRON_API_URL?</Empty>;
  }

  if (portfolios.length === 0) {
    return <Empty>No portfolios yet. Create one via the API, then import a CSV/OFX or sync IBKR Flex.</Empty>;
  }

  return (
    <div>
      <h1 className="text-2xl font-semibold tracking-tight">Portfolios</h1>
      <ul className="mt-4 divide-y divide-line rounded-lg border border-line">
        {portfolios.map((p) => (
          <li key={p.id}>
            <Link href={`/portfolios/${p.id}`} className="flex items-center justify-between px-4 py-3 hover:bg-slate-50">
              <span className="font-medium">{p.name}</span>
              <span className="text-sm text-muted">{p.base_currency}</span>
            </Link>
          </li>
        ))}
      </ul>
    </div>
  );
}
