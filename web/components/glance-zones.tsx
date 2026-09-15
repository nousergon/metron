// Glance screen zones 1, 3–6 (metron-ops#248). Zones 3–5 render whatever the server-side
// ranker yields — no widget per facet — so a newly registered facet appears unchanged.
// Every claim taps once to the surface that computed it.

import Link from "next/link";

import type { GlanceHeadline as Headline, GlanceIntegrity, GlanceItem, GlanceRankedZone } from "@/lib/api-glance";
import { moneyWhole, percent, signClass, signedMoneyWhole } from "@/lib/format";
import { ProvenanceBadge } from "@/components/glance-provenance";
import { GlanceReload } from "@/components/glance-reload";

export function GlanceHeadline({ h, base }: { h: Headline; base: string }) {
  return (
    <div>
      <div className="flex items-start justify-between gap-2">
        <Link href={`${base}/${h.surface}`} className="block min-w-0">
          <div className="truncate text-3xl font-semibold tabular-nums">
            {h.total_value === null ? "—" : moneyWhole(h.total_value, h.base_currency)}
          </div>
        </Link>
        <GlanceReload />
      </div>
      <div className="mt-1 flex flex-wrap items-center gap-2 text-sm">
        {h.day_change !== null ? (
          <span className={`tabular-nums ${signClass(h.day_change)}`}>
            {signedMoneyWhole(h.day_change, h.base_currency)}
            {h.day_pct !== null ? ` (${h.day_pct >= 0 ? "+" : ""}${percent(h.day_pct)})` : ""}
          </span>
        ) : (
          <span className="text-muted">{h.day_note ?? "Day change not available."}</span>
        )}
        <ProvenanceBadge provenance={h.day_provenance} asOf={h.day_as_of} />
      </div>
      <div className="mt-1 flex flex-wrap items-center gap-2 text-[11px] text-muted">
        <ProvenanceBadge provenance={h.value_provenance} asOf={h.last_sync ?? h.value_as_of} />
        {h.n_brokers > 1 ? (
          <span>
            {h.n_accounts} accounts · {h.n_brokers} brokers
          </span>
        ) : null}
      </div>
    </div>
  );
}

function ItemRow({ item, base, kind }: { item: GlanceItem; base: string; kind: GlanceRankedZone["key"] }) {
  return (
    <li>
      <Link
        href={`${base}/${item.surface}`}
        data-facet={item.facet_key}
        className="flex items-start justify-between gap-3 py-2 transition hover:bg-white/5"
      >
        {kind === "movers" && item.ticker ? (
          <span className="min-w-0">
            <span className="font-medium">{item.ticker}</span>
            <span className="ml-2 text-[11px] text-muted">
              {item.pct !== null ? `${item.pct >= 0 ? "+" : ""}${percent(item.pct)}` : ""}
            </span>
          </span>
        ) : (
          <span className="min-w-0 text-sm">{item.text}</span>
        )}
        <span className="flex shrink-0 flex-col items-end gap-1">
          {kind === "movers" && item.amount !== null ? (
            <span className={`tabular-nums text-sm ${signClass(item.amount)}`}>{signedMoneyWhole(item.amount)}</span>
          ) : null}
          <ProvenanceBadge provenance={item.provenance} asOf={item.event_date ?? item.as_of} />
        </span>
      </Link>
    </li>
  );
}

export function GlanceRanked({ zone, base }: { zone: GlanceRankedZone; base: string }) {
  return (
    <div>
      {zone.is_floor ? (
        <p className="text-sm text-muted" data-floor="true">
          {zone.floor_text}
        </p>
      ) : (
        <ul className="divide-y divide-line">
          {zone.items.map((item, i) => (
            <ItemRow key={`${item.facet_key}-${item.ticker ?? i}`} item={item} base={base} kind={zone.key} />
          ))}
        </ul>
      )}
      {zone.unavailable.length > 0 ? (
        <p className="mt-2 text-[11px] text-muted/80">Not available on this plan: {zone.unavailable.join(" · ")}</p>
      ) : null}
    </div>
  );
}

export function GlanceIntegrityZone({ i, base }: { i: GlanceIntegrity; base: string }) {
  return (
    <Link href={`${base}/${i.surface}`} className="flex items-start gap-2 text-sm" data-status={i.status}>
      <span
        aria-hidden="true"
        className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${i.healthy ? "bg-emerald-500" : "bg-amber-500"}`}
      />
      <span className="min-w-0">
        <span>{i.text}</span>
        {i.positions_as_of ? <span className="block text-[11px] text-muted">Positions as of {i.positions_as_of}</span> : null}
      </span>
    </Link>
  );
}
