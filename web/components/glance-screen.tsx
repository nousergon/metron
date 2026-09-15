// The glance screen (metron-ops#248 Stage A): one phone screen, six zones, from one fetch.
// Phone width (390 px) is the canonical layout; desktop only widens the column.

import type { ReactNode } from "react";

import type { Glance, GlanceState } from "@/lib/api-glance";
import { GlancePath } from "@/components/glance-path";
import { GlanceHeadline, GlanceIntegrityZone, GlanceRanked } from "@/components/glance-zones";

const STATE_LABEL: Record<GlanceState, string> = {
  pre_open: "Before the open",
  open: "Market open",
  post_close: "After the close",
};

function Zone({ id, title, children }: { id: string; title?: string; children: ReactNode }) {
  return (
    <section data-zone={id} aria-label={title ?? id} className="border-b border-line py-3 last:border-b-0">
      {title ? <h2 className="mb-1 text-[11px] uppercase tracking-[0.14em] text-muted">{title}</h2> : null}
      {children}
    </section>
  );
}

export function GlanceScreen({ g, portfolioId }: { g: Glance; portfolioId: string }) {
  const base = `/portfolios/${portfolioId}`;
  return (
    <div className="w-full min-w-0" data-state={g.state}>
      <p className="mt-2 text-[11px] uppercase tracking-[0.14em] text-muted">{STATE_LABEL[g.state]}</p>
      <Zone id="headline">
        <GlanceHeadline h={g.headline} base={base} />
      </Zone>
      <Zone id="path" title="Path">
        <GlancePath path={g.path} href={`${base}/${g.path.surface}`} />
      </Zone>
      <Zone id="movers" title="Movers">
        <GlanceRanked zone={g.movers} base={base} />
      </Zone>
      <Zone id="insights" title="Insights">
        <GlanceRanked zone={g.insights} base={base} />
      </Zone>
      <Zone id="ahead" title="Ahead">
        <GlanceRanked zone={g.ahead} base={base} />
      </Zone>
      <Zone id="integrity" title="Integrity">
        <GlanceIntegrityZone i={g.integrity} base={base} />
        {g.degraded.length > 0 ? (
          <p className="mt-2 text-[11px] text-amber-500">
            Some observations could not be computed just now ({g.degraded.length}); the rest of this screen is current.
          </p>
        ) : null}
      </Zone>
      <p className="pb-4 pt-2 text-[10px] text-muted/70">Observations about your portfolio, not recommendations.</p>
    </div>
  );
}
