import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Privacy Policy — Metron",
  description: "Metron Privacy Policy",
};

// Template-quality draft (attorney review deferred to public/paid launch — Brian's
// 2026-07-20 ruling, alpha-engine-config-I1611). Entity facts filled 2026-07-09
// (Nous Ergon LLC / WA).
const LAST_UPDATED = "July 22, 2026";

export default function PrivacyPage() {
  return (
    <article className="prose prose-invert max-w-3xl text-sm leading-relaxed text-fg">
      <h1 className="text-xl font-semibold">Privacy Policy</h1>
      <p className="text-muted">Last updated: {LAST_UPDATED}</p>

      <p>
        This Privacy Policy explains what data Metron (&ldquo;we,&rdquo; &ldquo;us&rdquo;) collects when you use our
        hosted dashboard, why we collect it, and what we do (and don&rsquo;t do) with it.
      </p>

      <h2 className="text-base font-semibold">1. What we collect</h2>
      <ul>
        <li>
          <strong>Account data:</strong> the email address you sign up with. We use email-link (&ldquo;magic
          link&rdquo;) authentication, so we do not store a password.
        </li>
        <li>
          <strong>Brokerage holdings and transactions:</strong> positions, balances, transaction history, and
          related account metadata, obtained either (a) via a read-only connection brokered by SnapTrade to your
          brokerage, or (b) from statement files (CSV/OFX/Flex) you upload yourself.
        </li>
        <li>
          <strong>Computed analytics:</strong> derived figures (returns, attribution, income, risk, tax lots)
          calculated from the data above and stored so your dashboard loads without recomputing from scratch.
        </li>
        <li>
          <strong>Basic operational logs:</strong> standard request/error logs needed to run and secure the
          Service. We do not use third-party analytics trackers or advertising pixels on the product surface.
        </li>
      </ul>
      <p>We do not collect more PII than the email address above — no name, address, or phone number is required.</p>

      <h2 className="text-base font-semibold">2. What we don&rsquo;t collect</h2>
      <p>
        We never see or store your brokerage login credentials. Brokerage authentication happens on your
        broker&rsquo;s own site via SnapTrade&rsquo;s OAuth-style flow; Metron only receives read-only account data
        after you approve the connection, and cannot place trades or move money.
      </p>

      <h2 className="text-base font-semibold">3. How we use your data</h2>
      <p>
        Solely to operate the Service for you: to sync and display your holdings/transactions, compute the
        analytics you see (performance, attribution, income, risk, scenarios, tax), and maintain your account. We
        do not use your financial data to train AI/ML models, and we do not sell, rent, or share your data with
        third parties for their marketing purposes.
      </p>

      <h2 className="text-base font-semibold">4. Who we share data with</h2>
      <p>
        We share the minimum necessary data with infrastructure providers strictly to run the Service, currently
        including: SnapTrade (brokerage connectivity), our database/hosting providers (e.g. Neon for Postgres, our
        application hosting provider), and our authentication provider. These providers process data on our behalf
        under their own security commitments and are not permitted to use your data for their own purposes. We do
        not share data with data brokers or advertisers.
      </p>

      <h2 className="text-base font-semibold">5. Data retention and deletion</h2>
      <p>
        We retain your account and synced data for as long as your account is active. If you disconnect a
        brokerage account, we stop syncing new data for it; previously synced history remains in your workspace
        until you delete it or close your account. If you delete your account, we delete your workspace data
        (holdings, transactions, computed analytics, and account record) within a reasonable operational window,
        except where retention is required by law.
      </p>

      <h2 className="text-base font-semibold">6. Security</h2>
      <p>
        Data is stored in a tenant-isolated workspace and encrypted in transit. Sensitive credentials required to
        maintain brokerage connections (SnapTrade user secrets) are stored encrypted at rest, scoped per account,
        and are never exposed to the client application.
      </p>

      <h2 className="text-base font-semibold">7. Your rights</h2>
      <p>
        You can access, export, or delete your data, and disconnect any brokerage connection, from within the
        Service. Depending on your jurisdiction (e.g. California under CCPA/CPRA, or the EU/UK under GDPR) you may
        have additional rights to access, correct, delete, or port your personal data, and to opt out of certain
        processing. To exercise these rights, contact us at privacy@nousergon.ai.
      </p>

      <h2 className="text-base font-semibold">8. Children</h2>
      <p>The Service is not directed to individuals under 18, and we do not knowingly collect data from them.</p>

      <h2 className="text-base font-semibold">9. Changes to this policy</h2>
      <p>
        We may update this Privacy Policy as the Service evolves. Material changes will be reflected by updating
        the &ldquo;Last updated&rdquo; date above.
      </p>

      <h2 className="text-base font-semibold">10. Contact</h2>
      <p>
        Questions about this policy or your data can be sent to privacy@nousergon.ai. This Service is operated by
        Nous Ergon LLC, a Washington State limited liability company.
      </p>

      <p className="mt-8 rounded border border-line bg-panel p-3 text-xs text-muted">
        Draft status: this is a template-quality document (not yet attorney-reviewed) prepared for the hosted beta.
        Attorney review is deliberately deferred until public/paid launch or the first advice-flavored feature,
        per Brian&rsquo;s 2026-07-20 ruling (alpha-engine-config-I1611) — until then this page, not a lawyer, is the
        governing draft for the beta. Tracked in nousergon/metron-ops#203 (supersedes the closed metron-ops#18).
      </p>
    </article>
  );
}
