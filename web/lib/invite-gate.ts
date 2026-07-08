// Invite gate — closes self-serve signup during the private beta (metron-ops#18 M2:
// legal/launch surface). Magic-link is the same endpoint for new and returning users
// (see auth-forms.tsx), so gating happens on the shared `/sign-in/magic-link` request
// itself, BEFORE better-auth issues a token or sends any email:
//
//   - An email that already has an account (a `user` row exists) always passes — this
//     gate only blocks *new* signups, never re-entry for people already inside.
//   - A brand-new email must present a valid, unused invite code (the `inviteCode`
//     table below) in the magic-link request's `metadata.inviteCode` field. Wrong/used/
//     missing code -> 403, no email sent, no verification token minted.
//
// The code is consumed here, at request time (atomically, via a single guarded SQL
// UPDATE — see the `updateMany` call below), not when the emailed link is later
// clicked. better-auth's magic-link plugin only persists `{email, name}` into the
// verification token it stores (see node_modules/better-auth .../magic-link/index.mjs)
// — `metadata` is passed straight through to `sendMagicLink` and never round-trips to
// the verify step — so there is no later hook that still has the invite code in hand.
// Gating at request time is the honest tradeoff this implies: a request that never
// completes (the email is never opened/clicked) still burns the code. That matches how
// most invite-gated magic-link products behave in practice (the code gates "who gets
// sent a link", not "who finishes clicking it") and avoids forking better-auth's
// plugin internals to smuggle extra state through the token.
//
// Default ON (INVITE_GATE_ENABLED unset or "true"). The issue's intent is to close
// signup NOW, not to ship a flag that defaults open; unlike the personal/beta deploy
// toggles in the root .env.example (SNAPTRADE_PERSONAL, FEED_ENTITLED, ...), which
// default open because they gate *deploy shape* not *access control*, this one is a
// pre-launch access gate and defaults closed (gate ON). Set INVITE_GATE_ENABLED=false
// only for local/dev convenience, or once the beta goes fully public.
//
// Code provisioning: no admin panel or seed/CLI tooling exists yet in this repo (no
// migrations dir either — better-auth's own tables are schema-driven off `betterAuth()`
// config, which is what this file plugs into via `schema`, so `inviteCode` is
// provisioned the exact same way the `user`/`session`/`verification` tables are).
// Until a real issuance UI exists, seed rows directly, e.g.:
//
//   sqlite3 auth.sqlite "INSERT INTO inviteCode (id, code, createdAt) \
//     VALUES (lower(hex(randomblob(16))), 'METRON-BETA-XXXX', datetime('now'));"
//
// Per-user/single-use invite codes issued from a real admin surface (rather than a
// handful of shared codes seeded by hand) are the natural next step — tracked as a
// follow-up, not punted silently: this table already supports it (one row per code,
// single-use, optional `note` for who it was issued to/for).

import { APIError } from "better-auth";
import { createAuthMiddleware } from "better-auth/api";
import type { BetterAuthPlugin } from "better-auth";

export const INVITE_GATE_MESSAGES = {
  INVITE_CODE_REQUIRED: "An invite code is required to sign up — Metron is in private beta.",
  INVALID_INVITE_CODE: "That invite code isn't valid or has already been used.",
} as const;

export function inviteGateEnabled(): boolean {
  return process.env.INVITE_GATE_ENABLED !== "false";
}

export function inviteGate(): BetterAuthPlugin {
  return {
    id: "invite-gate",
    schema: {
      inviteCode: {
        fields: {
          code: { type: "string", required: true, unique: true },
          createdAt: { type: "date", required: true },
          usedAt: { type: "date", required: false },
          usedByEmail: { type: "string", required: false },
          note: { type: "string", required: false },
        },
      },
    },
    hooks: {
      before: [
        {
          matcher: (ctx) => ctx.path === "/sign-in/magic-link",
          handler: createAuthMiddleware(async (ctx) => {
            if (!inviteGateEnabled()) return;

            const email = (ctx.body as { email?: unknown } | undefined)?.email;
            // Not just a missing-email check: this `hooks.before` middleware runs BEFORE
            // better-auth's own zod body validation for the endpoint, so a malformed
            // request (e.g. `email` as an object/number) reaches here as-is. Bail out to
            // the same "let the endpoint's own validation handle it" behavior rather than
            // passing a non-string into `findUserByEmail`, which calls `.toLowerCase()`
            // internally and would throw an unhandled 500 instead of a clean 400.
            if (typeof email !== "string" || !email) return;

            const existingUser = await ctx.context.internalAdapter.findUserByEmail(email);
            if (existingUser) return; // returning user: never gated, only new signups are

            const metadata = (ctx.body as { metadata?: Record<string, unknown> } | undefined)?.metadata;
            const inviteCode = typeof metadata?.inviteCode === "string" ? metadata.inviteCode.trim() : "";
            if (!inviteCode) {
              throw new APIError("FORBIDDEN", { message: INVITE_GATE_MESSAGES.INVITE_CODE_REQUIRED });
            }

            // Guarded update, not a plain findOne-then-update: the WHERE clause requires
            // usedAt IS NULL at the same statement that sets it, so this compiles to one
            // `UPDATE ... WHERE code = ? AND usedAt IS NULL` — two concurrent requests
            // for the same code can't both report success (only one UPDATE affects a
            // row; the loser sees `updated < 1`, same as an invalid code). Codes stay
            // in the table (not deleted) so usage is auditable via `usedByEmail`.
            const updated = await ctx.context.adapter.updateMany({
              model: "inviteCode",
              where: [
                { field: "code", value: inviteCode },
                { field: "usedAt", value: null },
              ],
              update: { usedAt: new Date(), usedByEmail: email },
            });
            if (updated < 1) {
              throw new APIError("FORBIDDEN", { message: INVITE_GATE_MESSAGES.INVALID_INVITE_CODE });
            }
          }),
        },
      ],
    },
  };
}
