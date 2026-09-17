"use client";

// Owner-only "External demo invites" Settings section (metron-ops-I323). Only ever
// rendered by the Settings page after a server-side owner probe succeeds (see
// app/portfolios/[id]/settings/page.tsx) — this component does no authorization itself,
// it just renders what the server already decided the caller may see.

import { useState, useTransition } from "react";
import {
  createExternalDemoInviteAction,
  revokeExternalDemoInviteAction,
} from "@/app/portfolios/[id]/external-demo-invites-actions";
import type { ExternalDemoCounters, ExternalDemoInvite } from "@/lib/api";
import { Empty, Table } from "@/components/ui";
import { withBasePath } from "@/lib/base-path";

function fmt(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function Status({ msg }: { msg: { ok: boolean; text: string } | null }) {
  if (!msg) return null;
  return <span className={`text-sm ${msg.ok ? "text-positive" : "text-negative"}`}>{msg.text}</span>;
}

function CreateInvite({ portfolioId, released }: { portfolioId: string; released: boolean }) {
  const [pending, start] = useTransition();
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  // The plaintext code is shown exactly once — the backend never returns it again after
  // this response, so losing this state loses the code for good (a fresh invite is the
  // only recovery, same as any other single-display secret).
  const [justCreated, setJustCreated] = useState<{ code: string; expiresAt: string } | null>(null);
  const [copied, setCopied] = useState(false);

  function create() {
    setMsg(null);
    setJustCreated(null);
    setCopied(false);
    start(async () => {
      const r = await createExternalDemoInviteAction(portfolioId);
      if (r.ok) {
        setJustCreated({ code: r.code, expiresAt: r.expiresAt });
      } else {
        setMsg({ ok: false, text: r.message });
      }
    });
  }

  // Fully-qualified so it's paste-ready into a conversation — the same origin the admin
  // is currently on, which is always the real deployed origin (never a hardcoded guess).
  const inviteUrl = justCreated
    ? `${window.location.origin}${withBasePath(`/invite?code=${encodeURIComponent(justCreated.code)}`)}`
    : null;

  async function copy() {
    if (!inviteUrl) return;
    try {
      await navigator.clipboard.writeText(inviteUrl);
      setCopied(true);
    } catch {
      // Clipboard permission denied/unavailable: the URL is still selectable text below,
      // so this is cosmetic only — no error surfaced.
      setCopied(false);
    }
  }

  return (
    <div className="mb-4 space-y-2">
      {!released ? (
        <p className="text-sm text-muted">
          External demo not released (waiting on market-data display licence, metron-ops#24). Invite
          creation stays disabled until then.
        </p>
      ) : null}
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          disabled={pending || !released}
          onClick={create}
          className="rounded bg-ink px-3 py-1 text-sm font-medium text-paper hover:bg-white disabled:opacity-50"
        >
          {pending ? "Creating…" : "Create invite"}
        </button>
        <Status msg={msg} />
      </div>
      {inviteUrl ? (
        <div className="rounded-lg border border-line bg-surface p-3 text-sm">
          <p className="font-medium">Shown once — copy it now. It can&apos;t be shown again.</p>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <code className="break-all rounded bg-ink/5 px-2 py-1 text-xs">{inviteUrl}</code>
            <button
              type="button"
              onClick={copy}
              className="rounded border border-line px-2 py-1 text-xs hover:bg-ink/5"
            >
              {copied ? "Copied" : "Copy"}
            </button>
          </div>
          <p className="mt-1 text-xs text-muted">Expires {fmt(justCreated!.expiresAt)}.</p>
        </div>
      ) : null}
    </div>
  );
}

function RevokeButton({ portfolioId, inviteId }: { portfolioId: string; inviteId: string }) {
  const [pending, start] = useTransition();
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  function revoke() {
    setMsg(null);
    start(async () => {
      const r = await revokeExternalDemoInviteAction(portfolioId, inviteId);
      if (!r.ok) setMsg({ ok: false, text: r.message });
    });
  }

  return (
    <div className="flex flex-col items-end gap-1">
      <button
        type="button"
        disabled={pending}
        onClick={revoke}
        className="rounded border border-line px-2 py-1 text-xs text-negative hover:bg-ink/5 disabled:opacity-50"
      >
        {pending ? "Revoking…" : "Revoke"}
      </button>
      <Status msg={msg} />
    </div>
  );
}

function InvitesTable({ portfolioId, invites }: { portfolioId: string; invites: ExternalDemoInvite[] | null }) {
  if (invites === null) {
    return <Empty>Invite list not measured — couldn&apos;t reach the backend just now. Reload to retry.</Empty>;
  }
  if (invites.length === 0) {
    return <Empty>No invites yet.</Empty>;
  }
  return (
    <Table head={["Created", "Expires", "Redeemed", "Live sessions", ""]}>
      {invites.map((inv) => (
        <tr key={inv.id} className="border-b border-line last:border-0">
          <td className="px-4 py-2">{fmt(inv.created_at)}</td>
          <td className="px-4 py-2 text-right">{fmt(inv.expires_at)}</td>
          <td className="px-4 py-2 text-right">{inv.redeemed_at ? fmt(inv.redeemed_at) : "No"}</td>
          <td className="px-4 py-2 text-right">{inv.live_session_count}</td>
          <td className="px-4 py-2 text-right">
            <RevokeButton portfolioId={portfolioId} inviteId={inv.id} />
          </td>
        </tr>
      ))}
    </Table>
  );
}

function CountersTable({ counters }: { counters: ExternalDemoCounters | null }) {
  if (counters === null) {
    return <Empty>Counters not measured — couldn&apos;t reach the backend just now. Reload to retry.</Empty>;
  }
  const tapRows = Object.entries(counters.locked_card_taps).sort(([a], [b]) => a.localeCompare(b));
  return (
    <Table head={["Metric", "Count"]}>
      <tr className="border-b border-line">
        <td className="px-4 py-2">Invites created</td>
        <td className="px-4 py-2 text-right">{counters.invites_created}</td>
      </tr>
      <tr className="border-b border-line">
        <td className="px-4 py-2">Sessions started</td>
        <td className="px-4 py-2 text-right">{counters.sessions_started}</td>
      </tr>
      {tapRows.map(([card, count], i) => (
        <tr key={card} className={i === tapRows.length - 1 ? "" : "border-b border-line"}>
          <td className="px-4 py-2 text-muted">Taps · {card.replace(/_/g, " ")}</td>
          <td className="px-4 py-2 text-right">{count}</td>
        </tr>
      ))}
    </Table>
  );
}

export function ExternalDemoInvitesSection({
  portfolioId,
  counters,
  invites,
}: {
  portfolioId: string;
  counters: ExternalDemoCounters;
  invites: ExternalDemoInvite[] | null;
}) {
  return (
    <div className="space-y-6">
      <CreateInvite portfolioId={portfolioId} released={counters.external_demo_released} />
      <InvitesTable portfolioId={portfolioId} invites={invites} />
      <div>
        <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">Funnel counters</h3>
        <CountersTable counters={counters} />
      </div>
    </div>
  );
}
