"use server";

// Server Actions for the owner-only "External demo invites" Settings section
// (metron-ops-I323, follow-up to metron-PR467/metron-ops-I310). Server-side so the
// admin's bearer credential never reaches the browser — the same reason every other
// Settings action in this directory is a Server Action, not a client-side fetch.
//
// Every backend call here goes through the OWNER-ONLY `_require_owner` dependency in
// api/routers/external_demo.py: a verified identity listed in
// EXTERNAL_DEMO_ADMIN_EMAILS. A non-admin who somehow calls one of these (the section
// isn't rendered for them, but a Server Action is still a reachable endpoint) gets the
// backend's 401/403 back as a failed ActionResult — never a client-side-only gate.

import { revalidatePath } from "next/cache";
import { createExternalDemoInvite, MetronApiError, revokeExternalDemoInvite } from "@/lib/api";
import { requireApiAuth } from "@/lib/session";

export type CreateInviteResult =
  | { ok: true; code: string; expiresAt: string }
  | { ok: false; message: string };

export type ActionResult = { ok: boolean; message: string };

function errorMessage(e: unknown): string {
  if (e instanceof MetronApiError) {
    // The backend's own detail is safe to show verbatim here (e.g. "The external demo
    // is not released.", "Owner only.") — no secrets or internal detail leak through it.
    return e.message;
  }
  return "Request failed — backend reachable?";
}

/** Mint one invite. The plaintext code is returned ONCE, in this response — the backend
 * stores only its hash, so it can never be re-shown after this call. */
export async function createExternalDemoInviteAction(portfolioId: string): Promise<CreateInviteResult> {
  try {
    const apiAuth = await requireApiAuth();
    const { code, expires_at } = await createExternalDemoInvite(apiAuth);
    revalidatePath(`/portfolios/${portfolioId}/settings`);
    return { ok: true, code, expiresAt: expires_at };
  } catch (e) {
    return { ok: false, message: errorMessage(e) };
  }
}

/** Revoke an invite. Deletes it and any session it minted — a session that was already
 * live is killed by this too, not just future redemption. */
export async function revokeExternalDemoInviteAction(portfolioId: string, inviteId: string): Promise<ActionResult> {
  try {
    const apiAuth = await requireApiAuth();
    await revokeExternalDemoInvite(apiAuth, inviteId);
    revalidatePath(`/portfolios/${portfolioId}/settings`);
    return { ok: true, message: "Invite revoked." };
  } catch (e) {
    return { ok: false, message: errorMessage(e) };
  }
}
