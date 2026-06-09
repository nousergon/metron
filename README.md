# Metron

**Portfolio analytics, measured.**

Metron is a multi-tenant dashboard for **institutional-grade portfolio analytics on
your real accounts** — true returns, attribution, factor risk, scenarios, income, and
tax clarity. **No AI, no ads/trackers, no advice, read-only.** We compute; we never
tell you what to trade.

A [Nous Ergon](https://nousergon.ai) product, hosted at `metron.nousergon.ai`.

## Open source

Metron is **AGPL-3.0**. Self-host it freely. The hosted service exists for the
zero-ops convenience (managed sync, hosting, updates) and the proprietary data feeds
that don't ship in this repo — that's the commercial side; the product itself is open.

The quant core (factor risk, attribution, returns, VaR/CVaR, riskstats) is **not**
duplicated here — it lives in the public, MIT-licensed
[`alpha-engine-lib`](https://pypi.org/project/alpha-engine-lib/) and is imported.

## Layout

One codebase, two top-level Python packages (+ a web frontend in PH2):

| Path | What |
|---|---|
| `portfolio_analytics/` | The pure engine. `domain/` (ledger, realized income, tax lots, stress), `broker_io/` (IBKR Flex, SnapTrade, transaction tranching), `ingestion/` (FDX canonical schema + bronze/silver store + broker connectors + `CanonicalReader`). No web/cloud coupling; fully unit-tested. |
| `api/` | FastAPI service + the multi-tenant Postgres schema (`api/db/models.py`) over the engine. Tenant-isolated via Postgres RLS in prod. |
| `web/` | Next.js + Tremor frontend — **lands in PH2.** |

Proprietary runtime bits (LLM advisor prompt templates, private signal feeds) are
**never committed** — they load at runtime from gitignored config, so a self-host
runs the full open product without them and the hosted product layers them on.

## Develop

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pytest                      # engine + API
uvicorn api.main:app --reload
# → http://127.0.0.1:8000/health  ·  /meta  ·  /docs
```

`DATABASE_URL` defaults to a local SQLite file (zero vendor cost). Point it at Postgres
(`postgresql+psycopg://…`) for production — no model changes required.

## Provenance

Metron is the successor to the personal RoboDashboard (Streamlit), whose
front-end-agnostic engine was extracted here on 2026-06-09. RoboDashboard is being
retired once Metron reaches feature parity.
