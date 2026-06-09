# portfolio-analytics-app *(working name — final name is a parked decision)*

The public, multi-tenant SaaS for institutional-grade portfolio analytics on real
accounts. **No AI, no ads/trackers, no advice, read-only.**

This repo is the **public product** from the commercialization plan
(`robodashboard/private/commercialization-plan-260609.md`). The personal Streamlit
RoboDashboard stays separate as the dogfood/validation deployment. Both consume the
shared [`portfolio-analytics`](../portfolio-analytics) engine.

## Status — PH0 scaffolding

- `api/` — FastAPI backend wrapping the `portfolio-analytics` engine.
- `api/db/models.py` — the multi-tenant Postgres schema (tenant, user, portfolio,
  account, security, transaction, position, price). Dev runs on **SQLite** (zero
  vendor cost); production targets Postgres (Neon/Supabase) with per-tenant RLS.
- Frontend (Next.js + Tremor) lands in **PH2** — not in this repo yet.

The build phases (PH0–PH5) and their gates live in the commercialization plan §6.

## Run (dev)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '../portfolio-analytics'   # the shared engine (editable)
pip install -e '.[dev]'
uvicorn api.main:app --reload
# → http://127.0.0.1:8000/health  and  /docs
```

`DATABASE_URL` defaults to a local SQLite file. Point it at Postgres for production
(`postgresql+psycopg://…`); no schema changes required.

## Cost posture

Nothing in this repo requires a paid subscription to develop or run locally. Paid
dependencies (SnapTrade auto-sync, a licensed EOD price feed, hosting) are deferred
to later phases per the plan's cost model (§3) and are gated behind explicit opt-in.
