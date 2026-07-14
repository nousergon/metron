"use client";

import { createAuthClient } from "better-auth/react";
import { magicLinkClient } from "better-auth/client/plugins";

// Client for the SHARED nousergon-auth identity service (metron-ops#179). Sign-in
// (magic link) happens against this cross-origin service; its session cookie is set on
// the parent domain (`.nousergon.ai`) so it rides along to every product host.
// `credentials: "include"` is required for that cookie to be sent/stored on
// cross-origin calls. The magicLink client plugin exposes `signIn.magicLink(...)`.
export const AUTH_URL: string = process.env.NEXT_PUBLIC_AUTH_URL ?? "https://auth.nousergon.ai";

export const authClient = createAuthClient({
  baseURL: AUTH_URL,
  plugins: [magicLinkClient()],
  fetchOptions: { credentials: "include" },
});

export const { signIn, signOut, useSession } = authClient;
