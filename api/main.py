"""FastAPI application entrypoint.

PH0 skeleton: health + meta surface that proves the shared ``portfolio-analytics``
engine is wired in, plus a stub portfolios router over the multi-tenant schema. Real
analytics endpoints land in PH1–PH3 per the commercialization plan.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.config import settings
from api.db.session import create_all
from api.plugins import active_plugins
from api.routers import macro, meta, portfolios
from api.services.demo import DEMO_TENANT_ID


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


@app.middleware("http")
async def _demo_read_only(request: Request, call_next):
    """The demo portfolio (metron-ops#42) is READ-ONLY — refuse any mutating request
    (anything but GET/HEAD/OPTIONS) addressed to the demo tenant, so a visitor exploring
    the sample can never edit, import into, delete, or refresh the shared fixture. One
    HTTP-layer chokepoint covers every mutation route uniformly. The server-side seed
    runs in-process (not over HTTP), so it is unaffected."""
    if request.method not in _SAFE_METHODS:
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

# Mount any out-of-tree premium plugins (metron-ops). Importing them here registers
# their ORM models on the shared Base *before* lifespan's create_all runs, so a
# plugin's tables are created on the dev/personal SQLite without a separate migration.
# A stock public deploy installs no plugins → this loop is a no-op and the surface
# above is the entire product. See api/plugins.py for the open-core boundary.
for _plugin in active_plugins():
    app.include_router(_plugin.router)
