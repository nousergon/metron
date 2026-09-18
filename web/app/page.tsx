import Link from "next/link";
import { redirect } from "next/navigation";
import { getPortfolios, MetronApiError, type Portfolio } from "@/lib/api";
import { requireApiAuth } from "@/lib/session";
import { Empty } from "@/components/ui";
import { CreatePortfolio } from "@/components/create-portfolio";
import { isReferencePortfolio } from "@/lib/demo";

export const dynamic = "force-dynamic";

export default async function HomePage() {
  const apiAuth = await requireApiAuth(); // redirects to /login when signed out

  let portfolios: Portfolio[] = [];
  try {
    portfolios = await getPortfolios(apiAuth);
  } catch (e) {
    const detail = e instanceof MetronApiError ? `(${e.status})` : "";
    return <Empty>Couldn&apos;t reach the Metron API {detail}. Is the backend running at METRON_API_URL?</Empty>;
  }

  // Post-auth landing route (metron-ops#248 / metron-ops-I328, Brian ruling 2026-07-30): a
  // tenant holding exactly one portfolio — the common shape ("one personal workspace per
  // user", PH4 auth) and the shape of the demo household (metron-ops#317) — lands directly
  // on its glance screen, one tap sooner than the old Holdings landing. Reuses the same
  // `redirect()` from "next/navigation" `requireApiAuth` already uses above, rather than a
  // parallel client-side or middleware redirect mechanism. A tenant with zero portfolios
  // still gets the create flow below (there is nothing to glance at yet — this is the
  // populated-portfolio-vs-no-connected-accounts split this route has to hold). A tenant
  // holding more than one portfolio keeps the picker list below: the glance screen is
  // whole-portfolio scope (metron-ops#248) and has no aggregate to redirect a multi-
  // portfolio tenant into.
  if (portfolios.length === 1) {
    redirect(`/portfolios/${portfolios[0].id}/glance`);
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
                className="flex items-center justify-between px-4 py-3 hover:bg-white/5"
              >
                <span className="flex items-center gap-2 font-medium">
                  {p.name}
                  {isReferencePortfolio(p.id) ? (
                    <span className="rounded-full border border-line px-2 py-0.5 text-[10px] uppercase tracking-wider text-muted">
                      Illustrative
                    </span>
                  ) : null}
                </span>
                <span className="text-sm text-muted">{p.base_currency}</span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
