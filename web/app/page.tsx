import Link from "next/link";
import { getPortfolios, MetronApiError, type Portfolio } from "@/lib/api";
import { requireTenantId } from "@/lib/session";
import { Empty } from "@/components/ui";
import { CreatePortfolio } from "@/components/create-portfolio";

export const dynamic = "force-dynamic";

export default async function HomePage() {
  const tenantId = await requireTenantId(); // redirects to /login when signed out

  let portfolios: Portfolio[] = [];
  try {
    portfolios = await getPortfolios(tenantId);
  } catch (e) {
    const detail = e instanceof MetronApiError ? `(${e.status})` : "";
    return <Empty>Couldn&apos;t reach the Metron API {detail}. Is the backend running at METRON_API_URL?</Empty>;
  }

  return (
    <div>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold tracking-tight">Your portfolios</h1>
        <CreatePortfolio />
      </div>

      {portfolios.length === 0 ? (
        <div className="mt-4">
          <Empty>No portfolios yet. Create one above, then import a CSV/OFX or sync IBKR Flex.</Empty>
        </div>
      ) : (
        <ul className="mt-4 divide-y divide-line rounded-lg border border-line">
          {portfolios.map((p) => (
            <li key={p.id}>
              <Link
                href={`/portfolios/${p.id}`}
                className="flex items-center justify-between px-4 py-3 hover:bg-slate-50"
              >
                <span className="font-medium">{p.name}</span>
                <span className="text-sm text-muted">{p.base_currency}</span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
