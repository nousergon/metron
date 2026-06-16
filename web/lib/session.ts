import { cookies, headers } from "next/headers";
import { redirect } from "next/navigation";
import { auth } from "@/lib/auth";
import { DEMO_COOKIE, DEMO_TENANT_ID } from "@/lib/demo";

// Server-side session helpers. Pages/actions call `requireTenantId()` to get the
// signed-in user's workspace (tenant) — or get redirected to /login.

export async function getSession() {
  return auth.api.getSession({ headers: headers() });
}

/** The signed-in user's tenant id. Falls back to the READ-ONLY demo tenant when the
 *  `/demo` cookie is set (no auth) so a prospect can explore without signing up; a real
 *  session always wins. Otherwise redirects to /login. */
export async function requireTenantId(): Promise<string> {
  const session = await getSession();
  const tenantId = (session?.user as { tenantId?: string } | undefined)?.tenantId;
  if (tenantId) return tenantId;
  if (cookies().get(DEMO_COOKIE)?.value === "1") return DEMO_TENANT_ID;
  redirect("/login");
}
