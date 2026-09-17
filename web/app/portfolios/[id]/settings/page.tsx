import {
  getExcludedAccounts,
  getExternalDemoCounters,
  getMeta,
  getPortfolio,
  getPreferences,
  listExternalDemoInvites,
  MetronApiError,
  type ExcludedAccount,
  type ExternalDemoCounters,
  type ExternalDemoInvite,
  type Preferences,
} from "@/lib/api";
import { getGoal, EMPTY_GOAL, type Goal } from "@/lib/api-goal";
import { Empty, Section, Table } from "@/components/ui";
import { AccountTagRow, BaseCurrencyForm, ExcludedAccountRow, PreferencesForm } from "@/components/settings-forms";
import { ExternalDemoInvitesSection } from "@/components/external-demo-invites-section";
import { GoalForm } from "@/components/goal-form";
import { navFeatureStates } from "@/lib/entitlements";
import { loadAccountsMeta } from "@/lib/account-meta";
import { requireApiAuth } from "@/lib/session";
import { ImportPanel } from "@/components/import-panel";
import { PortfolioNav } from "@/components/portfolio-nav";
import { ThemeToggle } from "@/components/theme-toggle";

/** The owner-only "External demo invites" section's data, or `null` when the signed-in
 * identity isn't an admin (metron-ops-I323). Reuses `GET /external-demo/counters` —
 * already owner-gated by `EXTERNAL_DEMO_ADMIN_EMAILS` — AS the admin check itself: the
 * web tier never holds the admin list, so a 401/403 here IS "not an admin," and the
 * section renders nothing rather than a disabled echo of itself. */
async function loadExternalDemoAdmin(
  apiAuth: string,
): Promise<{ counters: ExternalDemoCounters; invites: ExternalDemoInvite[] | null } | null> {
  let counters: ExternalDemoCounters;
  try {
    counters = await getExternalDemoCounters(apiAuth);
  } catch (e) {
    if (e instanceof MetronApiError && (e.status === 401 || e.status === 403)) return null;
    // Backend reachable but erroring some other way (5xx, network): admin status can't
    // be established. Failing the whole Settings page over an optional admin section
    // would be worse than hiding it, so this degrades to "no section" too — logged here
    // so the degrade isn't a silent swallow.
    console.error("external-demo admin probe failed", e);
    return null;
  }
  const invites = await listExternalDemoInvites(apiAuth).catch((e) => {
    console.error("external-demo invites list failed", e);
    return null; // rendered as "not measured" by the section, never an empty list
  });
  return { counters, invites };
}

export const dynamic = "force-dynamic";

export default async function SettingsPage(props: { params: Promise<{ id: string }> }) {
  const params = await props.params;
  const { id } = params;
  const apiAuth = await requireApiAuth();
  const featureStates = await navFeatureStates(apiAuth);

  let portfolio, accountsMeta, preferences: Preferences, excluded: ExcludedAccount[];
  try {
    [portfolio, accountsMeta, preferences, excluded] = await Promise.all([
      getPortfolio(apiAuth, id),
      loadAccountsMeta(apiAuth, id),
      getPreferences(apiAuth, id),
      getExcludedAccounts(apiAuth, id).then((r) => r.excluded),
    ]);
  } catch (e) {
    if (e instanceof MetronApiError && e.status === 404) {
      return <Empty>Portfolio not found.</Empty>;
    }
    return <Empty>Couldn&apos;t load settings. Is the backend running?</Empty>;
  }

  // Best-effort: a goal-fetch failure degrades to the empty (no-goal) state rather than
  // failing the whole Settings page — the form still renders and a save recovers it.
  const goal: Goal = await getGoal(apiAuth, id).catch(() => EMPTY_GOAL);

  // A transient meta-cache failure degrades to "no accounts" here (fail-open, like
  // entitlements) rather than a hard page error — the tag table just re-populates on
  // the next successful read.
  const accounts = accountsMeta ?? [];

  // Connector capabilities (stored IBKR Flex creds → one-click "Sync IBKR"). Best-effort:
  // a meta failure just falls back to the BYO-token form (metron-ops#82).
  const flexStored = await getMeta(apiAuth)
    .then((m) => m.connectors.flex_stored)
    .catch(() => false);

  const externalDemoAdmin = await loadExternalDemoAdmin(apiAuth);

  return (
    <div>
      <PortfolioNav portfolioId={id} navQuery="" featureStates={featureStates} />

      <h1 className="mt-3 text-lg font-semibold">Settings &amp; data</h1>
      <p className="text-sm text-muted">
        Imports &amp; broker connections, reporting currency, account tags, and investor preferences.
      </p>

      <Section title="Imports & connections" note="CSV / OFX / IBKR Flex / SnapTrade">
        <ImportPanel portfolioId={id} flexStored={flexStored} />
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

      <Section title="Retirement goal" note="user-authored — Metron never suggests the number">
        <GoalForm portfolioId={id} current={goal} />
      </Section>

      <Section title="Appearance" note="display theme (saved in this browser)">
        <ThemeToggle />
      </Section>

      {externalDemoAdmin ? (
        <Section title="External demo invites" note="owner only — create, list, revoke; funnel counters">
          <ExternalDemoInvitesSection
            portfolioId={id}
            counters={externalDemoAdmin.counters}
            invites={externalDemoAdmin.invites}
          />
        </Section>
      ) : null}
    </div>
  );
}
