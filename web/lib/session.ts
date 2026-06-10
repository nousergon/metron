import { headers } from "next/headers";
import { redirect } from "next/navigation";
import { auth } from "@/lib/auth";

// Server-side session helpers. Pages/actions call `requireTenantId()` to get the
// signed-in user's workspace (tenant) — or get redirected to /login.

export async function getSession() {
  return auth.api.getSession({ headers: headers() });
}

/** The signed-in user's tenant id, or a redirect to /login. */
export async function requireTenantId(): Promise<string> {
  const session = await getSession();
  const tenantId = (session?.user as { tenantId?: string } | undefined)?.tenantId;
  if (!tenantId) redirect("/login");
  return tenantId;
}
