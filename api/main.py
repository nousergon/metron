"""FastAPI application entrypoint.

PH0 skeleton: health + meta surface that proves the shared ``portfolio-analytics``
engine is wired in, plus a stub portfolios router over the multi-tenant schema. Real
analytics endpoints land in PH1–PH3 per the commercialization plan.
"""

from __future__ import annotations

import os
import re
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from krepis.logging import setup_logging

from api.config import settings
from api.db.session import create_all
from api.plugins import active_plugins
from api.routers import events, indices, macro, meta, portfolios, research_intel, tax_planning
from api.services.demo import DEMO_TENANT_ID, REFERENCE_PORTFOLIO_ID

# Structured logging + flow-doctor. Passing a flow-doctor.yaml attaches a
# FlowDoctorHandler at ERROR (off under pytest), so every log.error() in a
# request handler or lifespan routes through flow-doctor's capture -> dedupe
# dispatch without explicit plumbing. Module-top so import-time errors surface
# too. The yaml uses an s3-only notifier (no ${VAR} secrets) — deploy-safe with
# zero secret-resolution crash risk; an alert channel is a tracked follow-up.
# Non-edge wiring (logging) comes from the MIT krepis layer; metron pulls only
# the AGPL quant core from nousergon-lib.
_FLOW_DOCTOR_YAML = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "flow-doctor.yaml",
)
setup_logging("metron", flow_doctor_yaml=_FLOW_DOCTOR_YAML)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Dev/test convenience: create tables on boot. Production uses Alembic migrations.
    if settings.env != "prod":
        create_all()
    # Seed the canned read-only demo portfolio (idempotent). Best-effort: a seeding
    # failure WARNs but never blocks startup — the demo is secondary to the real product.
    if settings.demo_enabled:
        import logging

        from api.db.session import SessionLocal
        from api.services import demo

        try:
            with SessionLocal() as session:
                demo.ensure_demo_seeded(session)
                # Reference Rate showcase: attempt an initial live sync from the
                # published artifact. Best-effort — no artifact (dev/no-S3) creates
                # nothing (the portfolio materializes only once a real artifact is in
                # hand); the daily refresh retries. Never blocks boot.
                try:
                    demo.sync_reference_holdings(session)
                except Exception:  # noqa: BLE001 - live sync is best-effort
                    logging.getLogger("api.demo").warning(
                        "reference-rate initial sync failed — daily refresh will retry", exc_info=True
                    )
        except Exception:  # noqa: BLE001 - secondary path; must never crash boot
            logging.getLogger("api.demo").warning("demo seed failed — continuing without it", exc_info=True)
    yield


app = FastAPI(
    title="Metron",
    version="0.0.1",
    summary="Portfolio analytics, measured — no AI, no ads, no advice, read-only (public tier).",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# The Reference Rate showcase is now readable by every real tenant (not just the demo
# tenant that owns it — see api/routers/portfolios.py::_owned_portfolio), so a real
# tenant's own X-Tenant-Id header will never match DEMO_TENANT_ID below. This path-based
# check is the second, independent leg of the same read-only guard: it keys off the fixed
# portfolio id instead of the caller's tenant, so it protects the showcase regardless of
# who's asking. Every mutating route in api/routers/portfolios.py is `/portfolios/{id}/...`
# with no extra prefix, so a plain anchored match against the fixed id is reliable without
# needing real path-param parsing (unavailable at this layer, before routing).
_REFERENCE_PORTFOLIO_PATH = re.compile(rf"^/portfolios/{re.escape(str(REFERENCE_PORTFOLIO_ID))}(?:/|$)")


@app.middleware("http")
async def _demo_read_only(request: Request, call_next):
    """The demo portfolio (metron-ops#42) and the Reference Rate showcase are READ-ONLY —
    refuse any mutating request (anything but GET/HEAD/OPTIONS) addressed to either, so no
    tenant can ever edit, import into, delete, or refresh a shared fixture. One HTTP-layer
    chokepoint covers every mutation route uniformly. The server-side seed/sync runs
    in-process (not over HTTP), so it is unaffected."""
    if request.method not in _SAFE_METHODS:
        if _REFERENCE_PORTFOLIO_PATH.match(request.url.path):
            return JSONResponse(status_code=403, content={"detail": "The demo portfolio is read-only."})
        raw = request.headers.get("x-tenant-id")
        if raw:
            try:
                if uuid.UUID(raw) == DEMO_TENANT_ID:
                    return JSONResponse(status_code=403, content={"detail": "The demo portfolio is read-only."})
            except ValueError:
                pass  # malformed header → let the route's _tenant_id dependency 400 it
    return await call_next(request)


@app.get("/health", tags=["system"])
def health() -> dict:
    return {"status": "ok", "env": settings.env}


app.include_router(meta.router)
app.include_router(portfolios.router)
app.include_router(macro.router)
app.include_router(indices.router)
app.include_router(research_intel.router)
app.include_router(tax_planning.router)
app.include_router(events.router)

# Mount any out-of-tree premium plugins (metron-ops). Importing them here registers
# their ORM models on the shared Base *before* lifespan's create_all runs, so a
# plugin's tables are created on the dev/personal SQLite without a separate migration.
# A stock public deploy installs no plugins → this loop is a no-op and the surface
# above is the entire product. See api/plugins.py for the open-core boundary.
for _plugin in active_plugins():
    app.include_router(_plugin.router)
