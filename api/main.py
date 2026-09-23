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

from api import entitlements
from api.config import settings
from api.db.session import create_all, engine
from api.plugins import active_plugins
from api.routers import (
    events,
    glance,
    goal,
    indices,
    macro,
    market_board,
    me,
    meta,
    planning,
    portfolios,
    research_intel,
)
from api.routers import external_demo as external_demo_router
from api.services import external_demo
from api.services.demo import DEMO_TENANT_ID, REFERENCE_PORTFOLIO_ID
from api.services.demo_household import DEMO_HOUSEHOLD_PORTFOLIO_ID

# Structured logging + flow-doctor. Passing a flow-doctor.yaml attaches a
# FlowDoctorHandler at ERROR (off under pytest), so every log.error() in a
# request handler or lifespan routes through flow-doctor's capture -> dedupe
# dispatch without explicit plumbing. Module-top so import-time errors surface
# too. The yaml uses an s3-only notifier (no ${VAR} secrets) — deploy-safe with
# zero secret-resolution crash risk; an alert channel is a tracked follow-up.
# Non-edge wiring (logging) comes from the MIT krepis layer; metron pulls only
# the AGPL quant core from nousergon-lib.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Durable home for flow-doctor's dedupe/rate-limit store (metron-ops#273). Anchored to
# the repo root, not the CWD, because the yaml is resolved at import and a relative path
# would follow whatever directory the process happened to start in. Set with the
# environment able to override it, so a future non-repo layout has a lever; created here
# because flow-doctor opens the sqlite file immediately and will not make the parent.
# cache/ is gitignored and survives a deploy's git pull, which is the property this needs.
os.environ.setdefault("METRON_STATE_DIR", os.path.join(_REPO_ROOT, "cache"))
os.makedirs(os.environ["METRON_STATE_DIR"], exist_ok=True)
_FLOW_DOCTOR_YAML = os.path.join(_REPO_ROOT, "flow-doctor.yaml")
setup_logging("metron", flow_doctor_yaml=_FLOW_DOCTOR_YAML)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Auto-create tables only on SQLite (dev/test convenience).
    # Postgres schema is managed by Alembic — never auto-DDL a production DB
    # (metron-ops#202: an env-based gate diverged from dialect reality).
    if engine.dialect.name == "sqlite":
        create_all()
    # Seed the canned read-only Showcase Portfolio (idempotent) — the shell, its
    # frozen sample sleeve, and the legacy-portfolio cleanup all run unconditionally on
    # every boot, independent of S3 artifact availability, so the no-auth /demo entry
    # works even in a dev/no-S3 environment. Best-effort: a seeding failure WARNs but
    # never blocks startup — the showcase is secondary to the real product.
    if settings.demo_enabled:
        import logging

        from api.db.session import SessionLocal
        from api.services import demo

        try:
            with SessionLocal() as session:
                demo.ensure_reference_seeded(session)
                # Live sleeve: attempt an initial sync from the published artifact.
                # Best-effort — no artifact (dev/no-S3) creates nothing (the live
                # sleeve materializes only once a real artifact is in hand); the daily
                # refresh retries. Never blocks boot.
                try:
                    demo.sync_reference_holdings(session)
                except Exception:  # noqa: BLE001 - live sync is best-effort
                    logging.getLogger("api.demo").warning(
                        "reference-rate initial sync failed — daily refresh will retry", exc_info=True
                    )
        except Exception:  # noqa: BLE001 - secondary path; must never crash boot
            logging.getLogger("api.demo").warning("demo seed failed — continuing without it", exc_info=True)

        # The ICP-shaped Demo household (metron-ops-I317) — a second, separate demo
        # portfolio under the same demo tenant. Best-effort like the showcase above;
        # its own fixture is fully committed (no S3 artifact dependency at all).
        try:
            from api.services import demo_household

            with SessionLocal() as session:
                demo_household.ensure_demo_household_seeded(session)
        except Exception:  # noqa: BLE001 - secondary path; must never crash boot
            logging.getLogger("api.demo").warning(
                "demo household seed failed — continuing without it", exc_info=True
            )
    yield


app = FastAPI(
    title="Metron",
    version="0.0.1",
    summary="Portfolio analytics, measured — no ads, no advice, read-only. We compute; we never tell you what to trade.",
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

# The Showcase Portfolio is now readable by every real tenant (not just the demo
# tenant that owns it — see api/routers/portfolios.py::_owned_portfolio). Real tenants
# authenticate with a bearer JWT and send no X-Tenant-Id at all (metron-ops#179) — only
# the signup-free demo still sends it, so the header leg below still catches every demo
# mutation. This path-based check is the second, independent leg of the same read-only
# guard: it keys off the fixed portfolio id instead of the caller's tenant, so it
# protects the showcase regardless of who's asking. Every mutating route in api/routers/portfolios.py is `/portfolios/{id}/...`
# with no extra prefix, so a plain anchored match against the fixed id is reliable without
# needing real path-param parsing (unavailable at this layer, before routing).
#
# The Demo household (metron-ops-I317) is the SAME carve-out under a second fixed id
# (api/services/demo_household.py::DEMO_HOUSEHOLD_PORTFOLIO_ID, api/routers/portfolios.py's
# ``_owned_portfolio``) — one alternation covers both fixed demo portfolio ids.
_DEMO_READ_ONLY_PORTFOLIO_IDS = (REFERENCE_PORTFOLIO_ID, DEMO_HOUSEHOLD_PORTFOLIO_ID)
_REFERENCE_PORTFOLIO_PATH = re.compile(
    rf"^/portfolios/(?:{'|'.join(re.escape(str(pid)) for pid in _DEMO_READ_ONLY_PORTFOLIO_IDS)})(?:/|$)"
)

# Side-effect-free COMPUTE routes exempt from the read-only guard below
# (metron-ops-I322) — a demo viewer must be able to run these even though they're
# POSTs. Named constant, default-deny: a route is exempt only by an EXACT
# (method, path-template) match here, never by method alone (a wildcard "allow every
# POST" would also let through PUT/POST writes on other routers sharing the same
# portfolio-id prefix).
#
# Each entry's handler was checked (metron-ops-I322 review) to write no TENANT-SCOPED
# row for a demo portfolio:
#   - cash-to-targets / whatif (api/routers/planning.py): pure arithmetic over
#     already-persisted holdings + the user-authored ``plan_targets`` row — no
#     ``session.add``/``.commit`` anywhere in ``cash_to_targets.py`` / ``whatif_purchase.py``.
#   - risk/compute, attribution/compute (api/routers/portfolios.py): ``do_backfill``
#     writes ONLY into the GLOBAL, cross-tenant ``securities``/``price_bars`` cache
#     (factor/sector benchmark ETFs — SPY, MTUM/QUAL/USMV/VLUE/SIZE, the SPDR sector
#     ETFs) via ``api/services/prices.py::backfill_prices`` and
#     ``api/services/sectors.py::ensure_sectors`` — the SAME cache every real tenant's
#     own risk/attribution compute already populates. The demo household's own
#     ``DEMO-`` securities already carry a sector (seeded by
#     ``demo_household.SECURITY_META``) and have no ``yf_symbol``, so ``ensure_sectors``
#     skips them and ``backfill_prices``/the data-spine source fail-soft-skips a symbol
#     it can't resolve — no row for a ``DEMO-`` security is ever written by these
#     routes. Nothing here touches a tenant-scoped table (``plan_targets``,
#     ``retirement_goal``, ``transactions``, ``nav_snapshots``, ...).
# See tests/test_demo_compute_allowlist.py for the row-count-unchanged proof, run
# under both an owner session and an external-demo session.
_DEMO_COMPUTE_ALLOWLIST: frozenset[tuple[str, re.Pattern[str]]] = frozenset({
    ("POST", re.compile(r"^/portfolios/[^/]+/plan/cash-to-targets$")),
    ("POST", re.compile(r"^/portfolios/[^/]+/plan/whatif$")),
    ("POST", re.compile(r"^/portfolios/[^/]+/risk/compute$")),
    ("POST", re.compile(r"^/portfolios/[^/]+/attribution/compute$")),
})


def _demo_compute_allowed(method: str, path: str) -> bool:
    """Whether ``method path`` is on the side-effect-free compute allowlist above."""
    return any(method == allowed_method and pattern.match(path) for allowed_method, pattern in _DEMO_COMPUTE_ALLOWLIST)


@app.middleware("http")
async def _demo_read_only(request: Request, call_next):
    """The demo portfolio (metron-ops#42), the Showcase Portfolio, and the Demo
    household (metron-ops-I317) are READ-ONLY — refuse any mutating request (anything
    but GET/HEAD/OPTIONS) addressed to any of them, so no tenant can ever edit, import
    into, delete, or refresh a shared fixture. One HTTP-layer
    chokepoint covers every mutation route uniformly. The server-side seed/sync runs
    in-process (not over HTTP), so it is unaffected.

    EXCEPT the named, side-effect-free compute routes on ``_DEMO_COMPUTE_ALLOWLIST``
    (metron-ops-I322): cash-to-targets, what-if, and risk/attribution compute persist
    no tenant-scoped row, so a demo viewer may run them despite the POST method."""
    if request.method not in _SAFE_METHODS and not _demo_compute_allowed(request.method, request.url.path):
        if _REFERENCE_PORTFOLIO_PATH.match(request.url.path):
            return JSONResponse(status_code=403, content={"detail": "The demo portfolio is read-only."})
        raw = request.headers.get("x-tenant-id")
        if raw:
            try:
                if uuid.UUID(raw) == DEMO_TENANT_ID:
                    return JSONResponse(status_code=403, content={"detail": "The demo portfolio is read-only."})
            except ValueError:
                pass  # malformed header → the route's tenant dependency 401s it
    return await call_next(request)


@app.middleware("http")
async def _external_demo_pin(request: Request, call_next):
    """External user demo (metron-ops-I310): a request carrying ``X-Demo-Session`` is
    refused unless its route is on the default-deny allowlist, and otherwise resolves every
    entitlement against the server-side no-advice pin. Presence alone pins (it can only
    narrow); the token itself is verified by ``identity.require_tenant_id``. Design:
    ``api/services/external_demo.py``."""
    if external_demo.SESSION_HEADER not in request.headers:
        return await call_next(request)
    if not external_demo.is_route_allowed(request.method, request.url.path):
        return JSONResponse(status_code=403, content={"detail": "Not available in the demo."})
    token = entitlements.set_request_pin(external_demo.pin())
    try:
        return await call_next(request)
    finally:
        entitlements.reset_request_pin(token)


@app.middleware("http")
async def _clear_request_cache(request: Request, call_next):
    """Clear request-scoped caches (e.g., attractiveness universe) at end of each request.
    Prevents stale data leakage across requests while avoiding redundant S3 reads within
    a single request that spans multiple endpoints (metron-ops#142)."""
    try:
        return await call_next(request)
    finally:
        from api.services import attractiveness
        attractiveness.clear_request_cache()


@app.get("/health", tags=["system"])
def health() -> dict:
    return {"status": "ok", "env": settings.env}


app.include_router(me.router)
app.include_router(meta.router)
app.include_router(portfolios.router)
app.include_router(market_board.router)
app.include_router(planning.router)
app.include_router(goal.router)
app.include_router(macro.router)
app.include_router(indices.router)
app.include_router(research_intel.router)
app.include_router(events.router)
app.include_router(glance.router)
app.include_router(external_demo_router.router)

# Mount any out-of-tree premium plugins (metron-ops). Importing them here registers
# their ORM models on the shared Base *before* lifespan's create_all runs, so a
# plugin's tables are created on the dev/personal SQLite without a separate migration.
# A stock public deploy installs no plugins → this loop is a no-op and the surface
# above is the entire product. See api/plugins.py for the open-core boundary.
for _plugin in active_plugins():
    app.include_router(_plugin.router)
