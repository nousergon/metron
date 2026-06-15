"""Server-side enforcement of the entitlement matrix on the feed-dependent
endpoints (Phase 2b of the tier simulator, metron-ops#37).

Phase 1/2a built the model + `/meta/entitlements` + the UI nav lock; 2b makes the
**API itself** refuse to compute a feed-dependent feature when the active tier / data
feed excludes it — returning ``computable=false`` with the entitlement ``reason`` and
``required_tier`` (the upsell) instead of running the analytics. The gate fires before
any price/sector access, so it needs no fixtures.

The effective tier/feed is the deployment's (``default_tier`` +
``market_data_sync_enabled``); the ``X-Preview-*`` headers override them ONLY when
``tier_simulator`` is on (owner-only) — mirroring ``GET /meta/entitlements`` so the
simulator's feed toggle is honored server-side too.
"""

from __future__ import annotations

import uuid

import pytest

from api.config import settings

ENDPOINTS = [
    ("get", "risk"),
    ("post", "risk/compute"),
    ("get", "attribution"),
    ("post", "attribution/compute"),
]


@pytest.fixture()
def tenant() -> str:
    return str(uuid.uuid4())


def _portfolio(client, tenant: str) -> str:
    return client.post("/portfolios", json={"name": "P"}, headers={"X-Tenant-Id": tenant}).json()["id"]


def _call(client, method: str, tenant: str, pid: str, path: str, headers: dict | None = None):
    h = {"X-Tenant-Id": tenant, **(headers or {})}
    fn = client.get if method == "get" else client.post
    return fn(f"/portfolios/{pid}/{path}", headers=h)


@pytest.mark.parametrize(("method", "path"), ENDPOINTS)
def test_beta_tier_deployment_gates_on_tier(client, tenant, monkeypatch, method, path):
    """A beta-tier deployment doesn't include the wedge → upsell to pro (reason 'tier')."""
    monkeypatch.setattr(settings, "tier_simulator", False)
    monkeypatch.setattr(settings, "default_tier", "beta")
    monkeypatch.setattr(settings, "market_data_sync_enabled", True)
    pid = _portfolio(client, tenant)
    body = _call(client, method, tenant, pid, path).json()
    assert body["computable"] is False
    assert body["reason"] == "tier"
    assert body["required_tier"] == "pro"


@pytest.mark.parametrize(("method", "path"), ENDPOINTS)
def test_feed_off_deployment_gates_on_data(client, tenant, monkeypatch, method, path):
    """Pro/personal tier INCLUDES the wedge, but with no licensed feed it isn't
    computable → reason 'feed', still upsold to pro (the cheapest tier that bundles it)."""
    monkeypatch.setattr(settings, "tier_simulator", False)
    monkeypatch.setattr(settings, "default_tier", "personal")
    monkeypatch.setattr(settings, "market_data_sync_enabled", False)
    pid = _portfolio(client, tenant)
    body = _call(client, method, tenant, pid, path).json()
    assert body["computable"] is False
    assert body["reason"] == "feed"
    assert body["required_tier"] == "pro"


@pytest.mark.parametrize(("method", "path"), ENDPOINTS)
def test_entitled_deployment_passes_gate_to_the_analytics(client, tenant, monkeypatch, method, path):
    """Personal + feed on → entitled; the gate lets it through. The empty portfolio is
    not computable for a DATA reason (no prices), which is distinct from a gated reason:
    `required_tier` stays None (the entitlement matrix didn't block it)."""
    monkeypatch.setattr(settings, "tier_simulator", False)
    monkeypatch.setattr(settings, "default_tier", "personal")
    monkeypatch.setattr(settings, "market_data_sync_enabled", True)
    pid = _portfolio(client, tenant)
    body = _call(client, method, tenant, pid, path).json()
    assert body["required_tier"] is None
    assert body["reason"] != "tier" and body["reason"] != "feed"


@pytest.mark.parametrize(("method", "path"), ENDPOINTS)
def test_preview_headers_ignored_when_simulator_off(client, tenant, monkeypatch, method, path):
    """Preview headers must NOT let a non-owner re-scope: simulator off → ignored, so an
    entitled deployment still computes despite a beta/feed-off preview header."""
    monkeypatch.setattr(settings, "tier_simulator", False)
    monkeypatch.setattr(settings, "default_tier", "personal")
    monkeypatch.setattr(settings, "market_data_sync_enabled", True)
    pid = _portfolio(client, tenant)
    body = _call(
        client, method, tenant, pid, path,
        headers={"X-Preview-Tier": "beta", "X-Preview-Feed": "false"},
    ).json()
    assert body["required_tier"] is None  # preview ignored → not gated


@pytest.mark.parametrize(("method", "path"), ENDPOINTS)
def test_simulator_honors_preview_feed_off(client, tenant, monkeypatch, method, path):
    """Simulator on (owner): the feed-off preview header gates the endpoint server-side —
    the simulator's feed toggle is faithful all the way to the API."""
    monkeypatch.setattr(settings, "tier_simulator", True)
    monkeypatch.setattr(settings, "default_tier", "personal")
    monkeypatch.setattr(settings, "market_data_sync_enabled", True)
    pid = _portfolio(client, tenant)
    body = _call(client, method, tenant, pid, path, headers={"X-Preview-Feed": "false"}).json()
    assert body["computable"] is False
    assert body["reason"] == "feed"
    assert body["required_tier"] == "pro"


@pytest.mark.parametrize(("method", "path"), ENDPOINTS)
def test_simulator_preview_beta_gates_on_tier(client, tenant, monkeypatch, method, path):
    monkeypatch.setattr(settings, "tier_simulator", True)
    monkeypatch.setattr(settings, "default_tier", "personal")
    monkeypatch.setattr(settings, "market_data_sync_enabled", True)
    pid = _portfolio(client, tenant)
    body = _call(client, method, tenant, pid, path, headers={"X-Preview-Tier": "beta"}).json()
    assert body["computable"] is False
    assert body["reason"] == "tier"
    assert body["required_tier"] == "pro"


@pytest.mark.parametrize(("method", "path"), ENDPOINTS)
def test_simulator_bad_preview_tier_falls_back_not_500(client, tenant, monkeypatch, method, path):
    """A bogus preview tier must not 500 a compute call — fall back to the deployment
    entitlement (personal + feed on → not gated)."""
    monkeypatch.setattr(settings, "tier_simulator", True)
    monkeypatch.setattr(settings, "default_tier", "personal")
    monkeypatch.setattr(settings, "market_data_sync_enabled", True)
    pid = _portfolio(client, tenant)
    res = _call(client, method, tenant, pid, path, headers={"X-Preview-Tier": "bogus"})
    assert res.status_code == 200
    assert res.json()["required_tier"] is None
