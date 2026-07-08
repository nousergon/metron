// Invite gate — closes self-serve signup during the private beta (metron-ops#18).
// Exercises the plugin's `hooks.before` handler directly against a mocked better-auth
// `ctx`, mirroring the shape `createAuthMiddleware` hands the handler at runtime
// (ctx.body, ctx.context.internalAdapter, ctx.context.adapter): a full better-auth +
// SQLite boot isn't needed to verify the gate's decision logic.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { APIError } from "better-auth";
import { INVITE_GATE_MESSAGES, inviteGate } from "@/lib/invite-gate";

function buildCtx({
  email,
  inviteCode,
  existingUser = null,
  updated = 0,
}: {
  email?: unknown;
  inviteCode?: string;
  existingUser?: unknown;
  updated?: number;
}) {
  const findUserByEmail = vi.fn().mockResolvedValue(existingUser);
  const updateMany = vi.fn().mockResolvedValue(updated);
  const ctx = {
    body: {
      email,
      ...(inviteCode !== undefined ? { metadata: { inviteCode } } : {}),
    },
    context: {
      internalAdapter: { findUserByEmail },
      adapter: { updateMany },
    },
  };
  return { ctx, findUserByEmail, updateMany };
}

// The plugin registers exactly one `hooks.before` entry, matched on the magic-link
// sign-in path; grab its handler once per test via the plugin instance so a change to
// the matcher/handler wiring in lib/invite-gate.ts would break this import, not silently
// go untested.
function getHandler() {
  const plugin = inviteGate();
  const entry = plugin.hooks?.before?.[0];
  if (!entry) throw new Error("invite-gate: expected exactly one hooks.before entry");
  expect(entry.matcher({ path: "/sign-in/magic-link" } as never)).toBe(true);
  expect(entry.matcher({ path: "/sign-in/email" } as never)).toBe(false);
  return entry.handler;
}

describe("inviteGate", () => {
  const originalEnv = process.env.INVITE_GATE_ENABLED;

  beforeEach(() => {
    delete process.env.INVITE_GATE_ENABLED;
  });

  afterEach(() => {
    if (originalEnv === undefined) delete process.env.INVITE_GATE_ENABLED;
    else process.env.INVITE_GATE_ENABLED = originalEnv;
  });

  it("rejects a new signup with no invite code", async () => {
    const handler = getHandler();
    const { ctx, updateMany } = buildCtx({ email: "new@example.com" });

    await expect(handler(ctx as never)).rejects.toMatchObject({
      status: "FORBIDDEN",
      body: { message: INVITE_GATE_MESSAGES.INVITE_CODE_REQUIRED },
    });
    expect(updateMany).not.toHaveBeenCalled();
  });

  it("rejects a new signup with an invalid or already-used invite code", async () => {
    const handler = getHandler();
    // updateMany affecting 0 rows means the code didn't match an unused row.
    const { ctx } = buildCtx({ email: "new@example.com", inviteCode: "WRONG-CODE", updated: 0 });

    await expect(handler(ctx as never)).rejects.toMatchObject({
      status: "FORBIDDEN",
      body: { message: INVITE_GATE_MESSAGES.INVALID_INVITE_CODE },
    });
  });

  it("accepts a new signup with a valid, unused invite code and consumes it", async () => {
    const handler = getHandler();
    const { ctx, updateMany } = buildCtx({ email: "new@example.com", inviteCode: "GOOD-CODE", updated: 1 });

    await expect(handler(ctx as never)).resolves.toBeUndefined();
    expect(updateMany).toHaveBeenCalledWith(
      expect.objectContaining({
        model: "inviteCode",
        where: [
          { field: "code", value: "GOOD-CODE" },
          { field: "usedAt", value: null },
        ],
        update: expect.objectContaining({ usedByEmail: "new@example.com" }),
      }),
    );
  });

  it("never gates a returning user, even with no invite code", async () => {
    const handler = getHandler();
    const { ctx, updateMany } = buildCtx({
      email: "returning@example.com",
      existingUser: { user: { id: "u1", email: "returning@example.com" } },
    });

    await expect(handler(ctx as never)).resolves.toBeUndefined();
    expect(updateMany).not.toHaveBeenCalled();
  });

  it("trims whitespace around a submitted invite code", async () => {
    const handler = getHandler();
    const { ctx, updateMany } = buildCtx({ email: "new@example.com", inviteCode: "  GOOD-CODE  ", updated: 1 });

    await expect(handler(ctx as never)).resolves.toBeUndefined();
    expect(updateMany).toHaveBeenCalledWith(expect.objectContaining({ where: expect.arrayContaining([{ field: "code", value: "GOOD-CODE" }]) }));
  });

  it("is a no-op when INVITE_GATE_ENABLED=false, regardless of invite code", async () => {
    process.env.INVITE_GATE_ENABLED = "false";
    const handler = getHandler();
    const { ctx, findUserByEmail, updateMany } = buildCtx({ email: "new@example.com" });

    await expect(handler(ctx as never)).resolves.toBeUndefined();
    expect(findUserByEmail).not.toHaveBeenCalled();
    expect(updateMany).not.toHaveBeenCalled();
  });

  it("defaults to enabled when INVITE_GATE_ENABLED is unset", async () => {
    const handler = getHandler();
    const { ctx } = buildCtx({ email: "new@example.com" });

    await expect(handler(ctx as never)).rejects.toBeInstanceOf(APIError);
  });

  it("doesn't crash on a malformed (non-string) email — leaves it to the endpoint's own validation", async () => {
    // `hooks.before` runs ahead of better-auth's zod body validation, so a malformed
    // request body reaches this handler as-is. A naive truthiness check would pass a
    // non-string into `internalAdapter.findUserByEmail`, which calls `.toLowerCase()`
    // internally and throws — turning a bad request into an unhandled 500.
    const handler = getHandler();
    const { ctx, findUserByEmail } = buildCtx({ email: { not: "a string" } });

    await expect(handler(ctx as never)).resolves.toBeUndefined();
    expect(findUserByEmail).not.toHaveBeenCalled();
  });
});
