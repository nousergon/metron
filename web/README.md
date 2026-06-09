# Metron web

Next.js (App Router) frontend over the FastAPI backend in `../api`. Server Components
fetch the price-free read models and render them with Tailwind. Read-only; no AI, no
trackers.

## Pages (PH2)

- **`/`** — portfolios for the configured tenant.
- **`/portfolios/[id]`** — Portfolio + Income view: summary tiles (cost basis, realized
  gains, income, accounts), holdings, income-by-year, accounts.

Market value, unrealized P&L, and performance charts are **not** shown — they need a
licensed EOD price feed (PH1b, currently un-budgeted). Charting (Tremor) lands with
the Performance page when that feed exists. The UI states cost basis honestly rather
than fabricating valuations.

## Run

```
cp .env.example .env.local   # set METRON_API_URL + METRON_DEV_TENANT_ID
npm install
npm run dev                  # http://localhost:3000  (backend on :8000)
```

`METRON_DEV_TENANT_ID` is a placeholder until auth lands (PH4): the backend resolves
the tenant from an `X-Tenant-Id` header, which the server sends from this env var.

## Checks

```
npm run build      # production build (typecheck + compile)
npm run lint
npm run typecheck
```
