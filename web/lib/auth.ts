// Better Auth — email/password sign-in for Metron. One personal workspace per user:
// every new user is assigned their own `tenantId` (the backend's tenant grain) at
// creation, so a session resolves 1:1 to a workspace. Org/multi-seat is deferred
// until demand (the schema already supports tenant → users → portfolios).
//
// Auth identity lives here (its own SQLite/Postgres tables); the FastAPI backend
// stays the source of truth for portfolio data and trusts the server-set
// `X-Tenant-Id` header this app derives from the session.

import Database from "better-sqlite3";
import { randomUUID } from "node:crypto";
import { betterAuth } from "better-auth";

// Dev: a local SQLite file. Prod: point AUTH_DATABASE_URL at a Postgres URL and swap
// the dialect (deferred — the beta runs on SQLite).
const db = new Database(process.env.AUTH_DATABASE_URL ?? "./auth.sqlite");

export const auth = betterAuth({
  database: db,
  emailAndPassword: {
    enabled: true,
  },
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
});
