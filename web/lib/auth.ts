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
import { sendEmail } from "@/lib/email";

// Dev/personal: a local SQLite file. M2: point AUTH_DATABASE_URL at Postgres + swap the
// dialect (deferred — the beta runs on SQLite).
const db = new Database(process.env.AUTH_DATABASE_URL ?? "./auth.sqlite");

function magicLinkEmail(url: string): string {
  return [
    '<div style="font-family:system-ui,sans-serif;max-width:480px">',
    '<h2 style="font-weight:600">Sign in to Metron</h2>',
    "<p>Click the button below to sign in. This link expires shortly and can be used once.</p>",
    `<p><a href="${url}" style="display:inline-block;background:#111;color:#fff;`,
    'padding:10px 16px;border-radius:6px;text-decoration:none">Sign in</a></p>',
    '<p style="color:#666;font-size:13px">If you didn\'t request this, you can ignore this email.</p>',
    "</div>",
  ].join("");
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
    magicLink({
      // Server-side send of the one-time sign-in link. Throws (fails loud) when email
      // isn't configured, so a sign-in request surfaces the reason instead of a link
      // that never arrives.
      sendMagicLink: async ({ email, url }) => {
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
