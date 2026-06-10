"use client";

import { createAuthClient } from "better-auth/react";
import { magicLinkClient } from "better-auth/client/plugins";

// Same-origin base URL is inferred in the browser; the client talks to /api/auth/*.
// The magicLink client plugin exposes `signIn.magicLink({ email })`.
export const authClient = createAuthClient({
  plugins: [magicLinkClient()],
});

export const { signIn, signOut, useSession } = authClient;
