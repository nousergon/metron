"use client";

import { createAuthClient } from "better-auth/react";

// Same-origin base URL is inferred in the browser; the client talks to /api/auth/*.
export const authClient = createAuthClient();

export const { signIn, signUp, signOut, useSession } = authClient;
