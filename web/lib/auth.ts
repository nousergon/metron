// Better Auth — passwordless (magic link) sign-in for Metron. One personal workspace
// per user: every new user is assigned their own `tenantId` (the backend's tenant
// grain) at creation, so a session resolves 1:1 to a workspace. Org/multi-seat is
// deferred until demand (the schema already supports tenant → users → portfolios).
//
// Magic link (not user/password) is the deliberate auth method — the modern,
// lower-friction default (Slack/Notion/Figma): enter email → click the link → a
// persistent session, no password to create, reuse, or leak. The first link for a new
// email creates the account + workspace; later links just sign in. Google OAuth +
// passkeys can be added alongside later without changing this model.
//
// Auth identity lives here (its own SQLite/Postgres tables); the FastAPI backend stays
// the source of truth for portfolio data and trusts the server-set `X-Tenant-Id`
// header this app derives from the session.

import Database from "better-sqlite3";
import { randomUUID } from "node:crypto";
import { betterAuth } from "better-auth";
import { magicLink } from "better-auth/plugins";
import { inviteGate } from "@/lib/invite-gate";

// Dev/personal: a local SQLite file. M2: point AUTH_DATABASE_URL at Postgres + swap the
// dialect (deferred — the beta runs on SQLite).
const db = new Database(process.env.AUTH_DATABASE_URL ?? "./auth.sqlite");

// Branded HTML for the magic-link email. Table-based with inline styles — the only
// layout that renders consistently across email clients (Gmail, Apple Mail, Outlook);
// flexbox/grid and <style> blocks are unreliable there. The logo is the public Metron
// app icon served from the marketing site (metron.nousergon.ai is NOT behind Cloudflare
// Access, so email clients can fetch it); it carries its own dark background, so it
// renders cleanly on the light card.
const FONT =
  "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif";
const LOGO_URL = "https://metron.nousergon.ai/favicon-192.png";

function magicLinkEmail(url: string): string {
  return `<!doctype html>
<html>
<head><meta charset="utf-8" /><meta name="viewport" content="width=device-width,initial-scale=1" /></head>
<body style="margin:0;padding:0;background:#f4f4f5;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f5;">
    <tr><td align="center" style="padding:32px 16px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:480px;background:#ffffff;border:1px solid #e4e4e7;border-radius:12px;">
        <tr><td style="padding:32px 32px 0 32px;">
          <table role="presentation" cellpadding="0" cellspacing="0"><tr>
            <td style="vertical-align:middle;"><img src="${LOGO_URL}" width="40" height="40" alt="Metron" style="display:block;border-radius:8px;" /></td>
            <td style="vertical-align:middle;padding-left:12px;font-family:${FONT};font-size:18px;font-weight:600;color:#18181b;letter-spacing:-0.01em;">Metron</td>
          </tr></table>
        </td></tr>
        <tr><td style="padding:24px 32px 0 32px;font-family:${FONT};">
          <h1 style="margin:0 0 8px 0;font-size:20px;font-weight:600;color:#18181b;">Sign in to Metron</h1>
          <p style="margin:0;font-size:15px;line-height:22px;color:#52525b;">Click the button below to sign in. This link expires shortly and can be used once.</p>
        </td></tr>
        <tr><td style="padding:24px 32px 4px 32px;">
          <table role="presentation" cellpadding="0" cellspacing="0"><tr>
            <td style="border-radius:8px;background:#18181b;"><a href="${url}" style="display:inline-block;padding:12px 22px;font-family:${FONT};font-size:15px;font-weight:600;color:#ffffff;text-decoration:none;border-radius:8px;">Sign in</a></td>
          </tr></table>
        </td></tr>
        <tr><td style="padding:8px 32px 0 32px;font-family:${FONT};">
          <p style="margin:0;font-size:13px;line-height:20px;color:#71717a;">Or paste this link into your browser:<br/><a href="${url}" style="color:#0284c7;word-break:break-all;">${url}</a></p>
        </td></tr>
        <tr><td style="padding:24px 32px 32px 32px;font-family:${FONT};">
          <hr style="border:none;border-top:1px solid #e4e4e7;margin:0 0 16px 0;" />
          <p style="margin:0;font-size:12px;line-height:18px;color:#a1a1aa;">You're receiving this because a sign-in link was requested for this address. If that wasn't you, you can safely ignore this email &mdash; no action will be taken.</p>
          <p style="margin:12px 0 0 0;font-size:12px;color:#a1a1aa;">Nous Ergon &middot; Metron</p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>`;
}

export const auth = betterAuth({
  database: db,
  user: {
    additionalFields: {
      // The Metron tenant (workspace) this user owns. Set server-side at creation;
      // never user-supplied.
      tenantId: { type: "string", required: false, input: false },
    },
  },
  databaseHooks: {
    user: {
      create: {
        before: async (user) => ({
          data: { ...user, tenantId: randomUUID() },
        }),
      },
    },
  },
  plugins: [
    // Listed first: `inviteGate` registers a `hooks.before` middleware matched on
    // `/sign-in/magic-link` (see lib/invite-gate.ts) that throws for an un-invited new
    // signup. `hooks.before` runs ahead of the endpoint handler itself, so an un-invited
    // request is rejected before magicLink's handler ever mints a verification token or
    // calls `sendMagicLink` — no email goes out and no token is created.
    inviteGate(),
    magicLink({
      // Server-side send of the one-time sign-in link. Throws (fails loud) when email
      // isn't configured, so a sign-in request surfaces the reason instead of a link
      // that never arrives.
      sendMagicLink: async ({ email, url }) => {
        // Imported lazily so the static auth-config graph doesn't pull in `server-only`
        // (which the better-auth CLI can't resolve when generating/migrating schema).
        const { sendEmail } = await import("@/lib/email");
        await sendEmail({
          to: email,
          subject: "Sign in to Metron",
          html: magicLinkEmail(url),
          text: `Sign in to Metron: ${url}`,
        });
      },
    }),
  ],
});
