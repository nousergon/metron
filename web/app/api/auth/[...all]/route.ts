import { auth } from "@/lib/auth";
import { toNextJsHandler } from "better-auth/next-js";

// Better Auth's REST endpoints (sign-up, sign-in, get-session, sign-out, …) under
// /api/auth/*.
export const { GET, POST } = toNextJsHandler(auth.handler);
