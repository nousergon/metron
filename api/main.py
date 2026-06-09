"""FastAPI application entrypoint.

PH0 skeleton: health + meta surface that proves the shared ``portfolio-analytics``
engine is wired in, plus a stub portfolios router over the multi-tenant schema. Real
analytics endpoints land in PH1–PH3 per the commercialization plan.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.config import settings
from api.db.session import create_all
from api.routers import meta, portfolios


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Dev/test convenience: create tables on boot. Production uses Alembic migrations.
    if settings.env != "prod":
        create_all()
    yield


app = FastAPI(
    title="portfolio-analytics-app",
    version="0.0.1",
    summary="Institutional-grade portfolio analytics — no AI, no ads, no advice, read-only.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", tags=["system"])
def health() -> dict:
    return {"status": "ok", "env": settings.env}


app.include_router(meta.router)
app.include_router(portfolios.router)
