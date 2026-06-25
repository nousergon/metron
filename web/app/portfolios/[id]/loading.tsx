// Instant navigation feedback for every portfolio page. The portfolio routes are dynamic
// (per-tenant, account-scoped) and each makes several backend round-trips, so WITHOUT a
// Suspense boundary a nav click froze on the old page until the new one had fully rendered
// server-side — the "lag when switching pages". This loading.tsx IS that boundary: Next
// shows it the instant a link is clicked (and warms it via <Link> prefetch), so navigation
// feels immediate while the real page streams in behind it. One file at the [id] segment
// covers the Overview and every sub-page (Holdings / Performance / Tax / …).

/** A pulsing grey bar placeholder. */
function Bar({ className = "" }: { className?: string }) {
  return <div className={`rounded bg-line/60 ${className}`} />;
}

export default function PortfolioLoading() {
  return (
    <div className="animate-pulse" aria-busy="true" aria-label="Loading…">
      {/* Nav bar row: back link + Pages button. */}
      <div className="flex items-center justify-between gap-4">
        <Bar className="h-4 w-28" />
        <Bar className="h-8 w-28" />
      </div>

      {/* Headline card: total value + the two unrealized tiles. */}
      <div className="mt-6 rounded-lg border border-line p-5">
        <Bar className="h-3 w-24" />
        <Bar className="mt-3 h-8 w-48" />
        <Bar className="mt-2 h-3 w-32" />
        <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2">
          <div className="h-20 rounded-md border border-line" />
          <div className="h-20 rounded-md border border-line" />
        </div>
      </div>

      {/* Two stat cards. */}
      <div className="mt-4 grid grid-cols-2 gap-3">
        <div className="h-20 rounded-lg border border-line" />
        <div className="h-20 rounded-lg border border-line" />
      </div>

      {/* A table-ish block (accounts / holdings). */}
      <div className="mt-6 space-y-2">
        <div className="h-10 rounded-lg border border-line" />
        <div className="h-10 rounded-lg border border-line" />
        <div className="h-10 rounded-lg border border-line" />
      </div>
    </div>
  );
}
