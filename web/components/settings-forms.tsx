"use client";

// Settings editors (client) — base currency, per-account tags, and investor
// preferences. Each saves through a Server Action (tenant header stays server-side)
// and the action revalidates so the change paints across the portfolio views.

import { useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import {
  restoreExcludedAccountAction,
  savePreferencesAction,
  updateAccountTagsAction,
  updateBaseCurrencyAction,
} from "@/app/portfolios/[id]/actions";
import type { Account, ExcludedAccount, Preferences } from "@/lib/api";

const CURRENCIES = ["USD", "EUR", "GBP", "CAD", "AUD", "HKD", "JPY", "SGD", "CHF"];

function Status({ msg }: { msg: { ok: boolean; text: string } | null }) {
  if (!msg) return null;
  return <span className={`text-sm ${msg.ok ? "text-positive" : "text-negative"}`}>{msg.text}</span>;
}

export function BaseCurrencyForm({ portfolioId, current }: { portfolioId: string; current: string }) {
  const [value, setValue] = useState(current);
  const [pending, start] = useTransition();
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  function save() {
    setMsg(null);
    start(async () => {
      const r = await updateBaseCurrencyAction(portfolioId, value);
      setMsg({ ok: r.ok, text: r.message });
    });
  }

  return (
    <div className="flex flex-wrap items-center gap-2">
      <select
        className="rounded border border-line px-2 py-1 text-sm"
        value={value}
        onChange={(e) => setValue(e.target.value)}
      >
        {(CURRENCIES.includes(current) ? CURRENCIES : [current, ...CURRENCIES]).map((c) => (
          <option key={c} value={c}>
            {c}
          </option>
        ))}
      </select>
      <button
        type="button"
        disabled={pending || value === current}
        onClick={save}
        className="rounded bg-ink px-3 py-1 text-sm font-medium text-paper hover:bg-white disabled:opacity-50"
      >
        {pending ? "Saving…" : "Save"}
      </button>
      <Status msg={msg} />
      <span className="text-xs text-muted">All portfolio totals report in this currency.</span>
    </div>
  );
}

// Empty string = Auto (derive taxable status from the broker tags / keywords). The three
// explicit values map straight onto Account.tax_treatment and are authoritative.
const TAX_TREATMENTS: { value: string; label: string }[] = [
  { value: "", label: "Auto" },
  { value: "taxable", label: "Taxable" },
  { value: "tax_deferred", label: "Tax-deferred" },
  { value: "tax_exempt", label: "Tax-exempt" },
];

export function AccountTagRow({ portfolioId, account }: { portfolioId: string; account: Account }) {
  const [nickname, setNickname] = useState(account.nickname ?? "");
  const [institution, setInstitution] = useState(account.institution ?? "");
  const [accountType, setAccountType] = useState(account.account_type ?? "");
  // The 3-way type is authoritative; "" = Auto (clears any override on save).
  const [treatment, setTreatment] = useState(account.tax_treatment ?? "");
  const [pending, start] = useTransition();
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  function save() {
    setMsg(null);
    start(async () => {
      const r = await updateAccountTagsAction(portfolioId, account.account_id, {
        nickname: nickname.trim() || null,
        institution: institution.trim() || null,
        account_type: accountType.trim() || null,
        tax_treatment: treatment || null,
      });
      setMsg({ ok: r.ok, text: r.message });
    });
  }

  return (
    <tr className="border-b border-line last:border-0 align-top">
      <td className="px-4 py-2 font-medium text-muted">{account.name || account.external_id}</td>
      <td className="px-4 py-2">
        <input
          className="w-36 rounded border border-line px-2 py-1 text-sm"
          value={nickname}
          placeholder="e.g. My Roth"
          onChange={(e) => setNickname(e.target.value)}
        />
      </td>
      <td className="px-4 py-2">
        <input
          className="w-36 rounded border border-line px-2 py-1 text-sm"
          value={institution}
          placeholder="e.g. Fidelity"
          onChange={(e) => setInstitution(e.target.value)}
        />
      </td>
      <td className="px-4 py-2">
        <input
          className="w-36 rounded border border-line px-2 py-1 text-sm"
          value={accountType}
          placeholder="e.g. Roth IRA"
          onChange={(e) => setAccountType(e.target.value)}
        />
      </td>
      <td className="px-4 py-2">
        <select
          className="rounded border border-line px-2 py-1 text-sm"
          value={treatment}
          onChange={(e) => setTreatment(e.target.value)}
        >
          {TAX_TREATMENTS.map((t) => (
            <option key={t.value} value={t.value}>
              {t.label}
            </option>
          ))}
        </select>
      </td>
      <td className="px-4 py-2">
        <div className="flex items-center gap-2">
          <button
            type="button"
            disabled={pending}
            onClick={save}
            className="rounded bg-ink px-3 py-1 text-sm font-medium text-paper hover:bg-white disabled:opacity-50"
          >
            {pending ? "Saving…" : "Save"}
          </button>
          <Status msg={msg} />
        </div>
      </td>
    </tr>
  );
}

export function PreferencesForm({ portfolioId, current }: { portfolioId: string; current: Preferences }) {
  const [risk, setRisk] = useState(current.risk_tolerance ?? "");
  const [objective, setObjective] = useState(current.objective ?? "");
  const [notes, setNotes] = useState(current.notes ?? "");
  const [intradayEnabled, setIntradayEnabled] = useState(current.intraday_enabled ?? false);
  const [pending, start] = useTransition();
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  function save() {
    setMsg(null);
    start(async () => {
      const r = await savePreferencesAction(portfolioId, {
        risk_tolerance: risk || null,
        objective: objective || null,
        notes: notes.trim() || null,
        intraday_enabled: intradayEnabled,
      });
      setMsg({ ok: r.ok, text: r.message });
    });
  }

  return (
    <div className="max-w-xl space-y-3">
      <label className="block text-sm">
        <span className="text-muted">Risk tolerance</span>
        <select
          className="mt-1 block w-full rounded border border-line px-2 py-1"
          value={risk}
          onChange={(e) => setRisk(e.target.value)}
        >
          <option value="">—</option>
          <option value="conservative">Conservative</option>
          <option value="moderate">Moderate</option>
          <option value="aggressive">Aggressive</option>
        </select>
      </label>
      <label className="block text-sm">
        <span className="text-muted">Objective</span>
        <select
          className="mt-1 block w-full rounded border border-line px-2 py-1"
          value={objective}
          onChange={(e) => setObjective(e.target.value)}
        >
          <option value="">—</option>
          <option value="income">Income</option>
          <option value="growth">Growth</option>
          <option value="balanced">Balanced</option>
        </select>
      </label>
      <label className="block text-sm">
        <span className="text-muted">Notes</span>
        <textarea
          className="mt-1 block w-full rounded border border-line px-2 py-1"
          rows={3}
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
        />
      </label>
      <label className="flex items-start gap-2 text-sm">
        <input
          type="checkbox"
          className="mt-1"
          checked={intradayEnabled}
          onChange={(e) => setIntradayEnabled(e.target.checked)}
        />
        <span>
          <span className="text-muted">Live intraday prices</span>
          <span className="mt-0.5 block text-xs text-muted">
            Overlay ~15-min-delayed intraday prices on your holdings while the app is open. Off by
            default — values come from the official end-of-day close.
          </span>
        </span>
      </label>
      <div className="flex items-center gap-2">
        <button
          type="button"
          disabled={pending}
          onClick={save}
          className="rounded bg-ink px-3 py-1 text-sm font-medium text-paper hover:bg-white disabled:opacity-50"
        >
          {pending ? "Saving…" : "Save preferences"}
        </button>
        <Status msg={msg} />
      </div>
    </div>
  );
}

/** Deleted broker accounts — imports skip these; restoring re-imports on next sync. */
export function ExcludedAccountRow({ portfolioId, excluded }: { portfolioId: string; excluded: ExcludedAccount }) {
  const [pending, start] = useTransition();
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const router = useRouter();

  function restore() {
    setMsg(null);
    start(async () => {
      const r = await restoreExcludedAccountAction(portfolioId, excluded.key);
      setMsg({ ok: r.ok, text: r.message });
      if (r.ok) router.refresh();
    });
  }

  return (
    <tr className="border-b border-line last:border-0">
      <td className="px-3 py-2 font-mono text-xs">{excluded.external_id}</td>
      <td className="px-3 py-2 text-sm">{excluded.broker}</td>
      <td className="px-3 py-2">
        <div className="flex items-center gap-2">
          <button
            type="button"
            disabled={pending}
            onClick={restore}
            className="rounded border border-line px-2 py-0.5 text-xs hover:bg-white/5 disabled:opacity-50"
          >
            {pending ? "Restoring…" : "Restore"}
          </button>
          <Status msg={msg} />
        </div>
      </td>
    </tr>
  );
}
