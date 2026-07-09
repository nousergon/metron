import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Terms of Service — Metron",
  description: "Metron Terms of Service",
};

// DRAFT — pending counsel review. Entity facts filled 2026-07-09 (Nous Ergon LLC / WA).
// Do not link from signup or treat as published until reviewed by counsel (metron-ops#18).
const LAST_UPDATED = "July 9, 2026";

export default function TermsPage() {
  return (
    <article className="prose prose-invert max-w-3xl text-sm leading-relaxed text-fg">
      <h1 className="text-xl font-semibold">Terms of Service</h1>
      <p className="text-muted">Last updated: {LAST_UPDATED}</p>

      <p>
        These Terms of Service (&ldquo;Terms&rdquo;) govern your access to and use of Metron
        (&ldquo;Metron,&rdquo; &ldquo;the Service&rdquo;), operated by Nous Ergon LLC, a Washington State
        limited liability company (&ldquo;we,&rdquo; &ldquo;us,&rdquo; &ldquo;our&rdquo;). By creating an account or
        otherwise using the Service, you agree to these Terms. If you do not agree, do not use the Service.
      </p>

      <h2 className="text-base font-semibold">1. What Metron is</h2>
      <p>
        Metron is a read-only portfolio analytics dashboard. It connects to your brokerage accounts (via SnapTrade
        or imported statement files) to compute performance, attribution, income, risk, and tax reporting on
        holdings and transactions you already own. Metron does not place trades, does not manage assets, does not
        take custody of funds or securities, and does not act as a broker-dealer, investment adviser, or custodian.
      </p>

      <h2 className="text-base font-semibold">2. Not investment advice</h2>
      <p>
        Metron is a descriptive analytics tool. Nothing in the Service constitutes investment, financial, tax, or
        legal advice, and nothing in the Service is a recommendation to buy, sell, or hold any security. We are not
        a fiduciary to you. Figures shown (returns, attribution, risk, scenarios, tax lots) are computed from data
        you connect or import and may contain errors, delays, or gaps in coverage — verify anything you rely on
        against your broker&rsquo;s official records before acting on it. You are solely responsible for your
        investment decisions.
      </p>

      <h2 className="text-base font-semibold">3. Brokerage connections</h2>
      <p>
        When you connect a brokerage account, the connection is brokered by a third-party provider (currently
        SnapTrade) using read-only, broker-side authentication. Your brokerage login credentials are never entered
        into or stored by Metron. You may disconnect a linked account at any time from within the Service, which
        stops future syncing; see our Privacy Policy for what happens to previously synced data.
      </p>

      <h2 className="text-base font-semibold">4. Your account</h2>
      <p>
        You must provide a valid email address to create an account and are responsible for maintaining the
        security of access to that email, since sign-in is by emailed link. You must be at least 18 years old to
        use the Service. One workspace is provisioned per account; you are responsible for the accuracy of any data
        you import manually (CSV/OFX/Flex files).
      </p>

      <h2 className="text-base font-semibold">5. Acceptable use</h2>
      <p>
        You agree not to: use the Service to violate any law; attempt to access another user&rsquo;s workspace or
        data; scrape, reverse-engineer, or resell the Service outside the terms of the applicable open-source
        license (see Section 6); or interfere with the Service&rsquo;s operation or security.
      </p>

      <h2 className="text-base font-semibold">6. Open source</h2>
      <p>
        The Metron analytics engine is published under the AGPL-3.0 license and you are free to self-host it
        subject to that license&rsquo;s terms. These Terms govern only your use of the hosted Service we operate at
        our domain(s); they do not apply to your own self-hosted deployment.
      </p>

      <h2 className="text-base font-semibold">7. Disclaimers and limitation of liability</h2>
      <p>
        THE SERVICE IS PROVIDED &ldquo;AS IS&rdquo; WITHOUT WARRANTIES OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING
        WARRANTIES OF ACCURACY, MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE, OR NON-INFRINGEMENT. TO THE
        MAXIMUM EXTENT PERMITTED BY LAW, Nous Ergon LLC AND ITS OPERATORS WILL NOT BE LIABLE FOR ANY INDIRECT,
        INCIDENTAL, SPECIAL, CONSEQUENTIAL, OR PUNITIVE DAMAGES, OR ANY LOSS OF PROFITS OR INVESTMENT LOSSES,
        ARISING FROM YOUR USE OF OR RELIANCE ON THE SERVICE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGES. OUR
        TOTAL LIABILITY FOR ANY CLAIM ARISING OUT OF THESE TERMS OR THE SERVICE WILL NOT EXCEED THE GREATER OF
        $100 OR THE AMOUNT YOU PAID US IN THE 12 MONTHS BEFORE THE CLAIM AROSE.
      </p>

      <h2 className="text-base font-semibold">8. Termination</h2>
      <p>
        You may stop using the Service and request account deletion at any time. We may suspend or terminate access
        for violation of these Terms or for operational/security reasons, with notice where practical.
      </p>

      <h2 className="text-base font-semibold">9. Changes</h2>
      <p>
        We may update these Terms as the Service evolves. Material changes will be reflected by updating the
        &ldquo;Last updated&rdquo; date above; continued use after changes take effect constitutes acceptance.
      </p>

      <h2 className="text-base font-semibold">10. Contact / governing law</h2>
      <p>
        These Terms are governed by the laws of Washington State, without regard to conflict-of-laws
        principles. Questions about these Terms can be sent to privacy@nousergon.ai.
      </p>

      <p className="mt-8 rounded border border-line bg-panel p-3 text-xs text-muted">
        Draft status: this page is a working draft prepared for legal review. Entity facts filled (Nous Ergon LLC,
        WA; privacy@nousergon.ai) pending counsel sign-off before this route is linked from signup or made
        publicly indexable. Tracked in nousergon/metron-ops#18.
      </p>
    </article>
  );
}
