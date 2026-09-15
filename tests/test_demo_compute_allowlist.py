"""Demo compute allowlist (metron-ops-I322): cash-to-targets, what-if, and
risk/attribution compute stay reachable on the demo household despite the read-only
guard and the external-demo route allowlist — proven by (1) a before/after row-count
snapshot over every TENANT-SCOPED table, run under both an owner session and an
external-demo session (the PR467 pin), and (2) an end-to-end check that both session
kinds get a non-empty plan back, now that plan targets are seeded
(``demo_household.DEMO_HOUSEHOLD_PLAN_TARGETS``)."""

from __future__ import annotations

import uuid

import pytest

from api.config import settings
from api.db import models
from api.services import demo_household
from api.services import external_demo as svc

HOUSEHOLD = str(demo_household.DEMO_HOUSEHOLD_PORTFOLIO_ID)
OWNER_TENANT = str(uuid.uuid4())  # any tenant — _owned_portfolio special-cases the household id regardless of caller
OWNER_HEADERS = {"X-Tenant-Id": OWNER_TENANT}

# (method, path, json body) for every route on the allowlist (api/main.py::_DEMO_COMPUTE_ALLOWLIST).
COMPUTE_ROUTES: tuple[tuple[str, str, dict | None], ...] = (
    ("POST", f"/portfolios/{HOUSEHOLD}/plan/cash-to-targets", {"amount_usd": 500.0}),
    ("POST", f"/portfolios/{HOUSEHOLD}/plan/whatif", {"symbol": "DEMO-AAPL", "amount_usd": 500.0}),
    ("POST", f"/portfolios/{HOUSEHOLD}/risk/compute", None),
    ("POST", f"/portfolios/{HOUSEHOLD}/attribution/compute", None),
)


def _tenant_scoped_models() -> list[type]:
    """Every ORM model carrying a ``tenant_id`` column — introspected (not hand-listed)
    so a table added later is covered automatically, same universe the production
    per-tenant RLS policy (``api/db/models.py`` module docstring) applies to."""
    return [
        mapper.class_
        for mapper in models.Base.registry.mappers
        if "tenant_id" in mapper.columns
    ]


def _row_counts(session) -> dict[str, int]:
    return {cls.__name__: session.query(cls).count() for cls in _tenant_scoped_models()}


@pytest.fixture()
def seeded(db_session):
    demo_household.ensure_demo_household_seeded(db_session)
    return db_session


@pytest.fixture()
def released(monkeypatch):
    monkeypatch.setattr(settings, "external_demo_released", True)


@pytest.fixture()
def external_headers(released, seeded):
    """An external-demo session over the same DB the row-count snapshot reads —
    mirrors ``tests/test_external_demo.py``'s ``demo_headers`` fixture."""
    code, _ = svc.create_invite(seeded)
    token, _ = svc.redeem_invite(seeded, code)
    return {"X-Demo-Session": token}


# ── No-write proof (deliverable 2) ────────────────────────────────────────────
@pytest.mark.parametrize("method, path, body", COMPUTE_ROUTES, ids=[p for _, p, _ in COMPUTE_ROUTES])
def test_owner_session_compute_routes_write_no_tenant_scoped_row(client, seeded, method, path, body):
    before = _row_counts(seeded)
    r = client.request(method, path, json=body, headers=OWNER_HEADERS)
    assert r.status_code == 200, r.text
    after = _row_counts(seeded)
    assert after == before


@pytest.mark.parametrize("method, path, body", COMPUTE_ROUTES, ids=[p for _, p, _ in COMPUTE_ROUTES])
def test_external_demo_session_compute_routes_write_no_tenant_scoped_row(
    raw_client, seeded, external_headers, method, path, body
):
    before = _row_counts(seeded)
    r = raw_client.request(method, path, json=body, headers=external_headers)
    assert r.status_code == 200, r.text
    after = _row_counts(seeded)
    assert after == before


# ── Persisting writes still refused (deliverable 5 — the other half of the same guard) ──
@pytest.mark.parametrize(
    "method, path, body",
    [
        ("PUT", f"/portfolios/{HOUSEHOLD}/plan/targets", {"targets": [{"symbol": "TSLA", "weight": 1.0}]}),
        ("PUT", f"/portfolios/{HOUSEHOLD}/goal", {"target_amount_usd": 1.0}),
        ("PATCH", f"/portfolios/{HOUSEHOLD}", {"name": "Hijacked"}),
    ],
    ids=["put-plan-targets", "put-goal", "patch-rename"],
)
def test_owner_session_persisting_writes_still_403(client, seeded, method, path, body):
    r = client.request(method, path, json=body, headers=OWNER_HEADERS)
    assert r.status_code == 403


@pytest.mark.parametrize(
    "method, path, body",
    [
        ("PUT", f"/portfolios/{HOUSEHOLD}/plan/targets", {"targets": [{"symbol": "TSLA", "weight": 1.0}]}),
        ("PUT", f"/portfolios/{HOUSEHOLD}/goal", {"target_amount_usd": 1.0}),
        ("PATCH", f"/portfolios/{HOUSEHOLD}", {"name": "Hijacked"}),
    ],
    ids=["put-plan-targets", "put-goal", "patch-rename"],
)
def test_external_demo_session_persisting_writes_still_403(raw_client, external_headers, method, path, body):
    r = raw_client.request(method, path, json=body, headers=external_headers)
    assert r.status_code == 403


# ── End-to-end: both session kinds get a real plan (Closes-when) ─────────────
def test_owner_session_gets_a_nonempty_cash_to_targets_plan(client, seeded):
    r = client.post(f"/portfolios/{HOUSEHOLD}/plan/cash-to-targets", json={"amount_usd": 1000.0}, headers=OWNER_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is True
    assert body["lines"], "expected at least one allocation line"
    assert body["allocated_usd"] > 0


def test_owner_session_gets_a_nonempty_whatif_plan(client, seeded):
    r = client.post(
        f"/portfolios/{HOUSEHOLD}/plan/whatif", json={"symbol": "DEMO-AAPL", "amount_usd": 1000.0}, headers=OWNER_HEADERS
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["shares"] > 0
    assert body["usd"] > 0


def test_external_demo_session_gets_a_nonempty_cash_to_targets_plan(raw_client, external_headers):
    r = raw_client.post(
        f"/portfolios/{HOUSEHOLD}/plan/cash-to-targets", json={"amount_usd": 1000.0}, headers=external_headers
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is True
    assert body["lines"], "expected at least one allocation line"
    assert body["allocated_usd"] > 0


def test_external_demo_session_gets_a_nonempty_whatif_plan(raw_client, external_headers):
    r = raw_client.post(
        f"/portfolios/{HOUSEHOLD}/plan/whatif",
        json={"symbol": "DEMO-AAPL", "amount_usd": 1000.0},
        headers=external_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["shares"] > 0
    assert body["usd"] > 0
