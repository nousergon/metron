import { getAccounts, getExcludedAccounts, getPortfolio, getPreferences, MetronApiError, type ExcludedAccount, type Preferences } from "@/lib/api";
import { Empty, Section, Table } from "@/components/ui";
import { AccountTagRow, BaseCurrencyForm, ExcludedAccountRow, PreferencesForm } from "@/components/settings-forms";
import { requireTenantId } from "@/lib/session";
import { ImportPanel } from "@/components/import-panel";
import { PortfolioNav } from "@/components/portfolio-nav";
import { ThemeToggle } from "@/components/theme-toggle";

export const dynamic = "force-dynamic";

export default async function SettingsPage({ params }: { params: { id: string } }) {
  const { id } = params;
  const tenantId = await requireTenantId();

  let portfolio, accounts, preferences: Preferences, excluded: ExcludedAccount[];
  try {
    [portfolio, accounts, preferences, excluded] = await Promise.all([
      getPortfolio(tenantId, id),
      getAccounts(tenantId, id),
      getPreferences(tenantId, id),
      getExcludedAccounts(tenantId, id).then((r) => r.excluded),
    ]);
  } catch (e) {
    if (e instanceof MetronApiError && e.status === 404) {
      return <Empty>Portfolio not found.</Empty>;
    }
    return <Empty>Couldn&apos;t load settings. Is the backend running?</Empty>;
  }

  return (
    <div>
      <PortfolioNav portfolioId={id} navQuery="" />

      <h1 className="mt-3 text-lg font-semibold">Settings &amp; data</h1>
      <p className="text-sm text-muted">
        Imports &amp; broker connections, reporting currency, account tags, and investor preferences.
      </p>

      <Section title="Imports & connections" note="CSV / OFX / IBKR Flex / SnapTrade">
        <ImportPanel portfolioId={id} />
      </Section>

      <Section title="Base currency" note="reporting currency for all totals">
        <BaseCurrencyForm portfolioId={id} current={portfolio.base_currency} />
      </Section>

      <Section title="Accounts" note="set a nickname, institution, and tax treatment (Auto derives from the broker)">
        {accounts.length === 0 ? (
          <Empty>No connected accounts yet.</Empty>
        ) : (
          <Table head={["Account", "Nickname", "Institution", "Account type", "Tax treatment", "Save"]}>
            {accounts.map((a) => (
              <AccountTagRow key={a.account_id} portfolioId={id} account={a} />
            ))}
          </Table>
        )}
      </Section>

      {excluded.length > 0 ? (
        <Section
          title="Deleted broker accounts"
          note="syncs skip these — restore one, then run a sync to re-import it"
        >
          <Table head={["Account number", "Source", ""]}>
            {excluded.map((e) => (
              <ExcludedAccountRow key={e.key} portfolioId={id} excluded={e} />
            ))}
          </Table>
        </Section>
      ) : null}

      <Section title="Investor preferences">
        <PreferencesForm portfolioId={id} current={preferences} />
      </Section>

      <Section title="Appearance" note="display theme (saved in this browser)">
        <ThemeToggle />
      </Section>
    </div>
  );
}
