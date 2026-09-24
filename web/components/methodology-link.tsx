// "How this is measured" affordance (metron-ops-I205): a quiet header link from an
// analytics page to the matching section of the public /methodology page. <Link> so the
// deployed basePath is applied automatically.

import Link from "next/link";

export type MethodologySection =
  | "valuation"
  | "performance"
  | "risk-measures"
  | "attribution"
  | "factor-risk"
  | "diagnostics"
  | "income"
  | "tax-lots"
  | "technical-rating";

export function MethodologyLink({ section }: { section: MethodologySection }) {
  return (
    <Link href={`/methodology#${section}`} className="text-[11px] text-muted underline-offset-2 hover:text-fg hover:underline">
      How this is measured
    </Link>
  );
}
