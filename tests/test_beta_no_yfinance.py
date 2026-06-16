"""Beta tier makes ZERO yfinance-derived calls (metron-ops#52).

Two invariants:
1. metron's own code NEVER imports yfinance — every market-data source routes through the
   S3 data-spine (whose data is yfinance-derived UPSTREAM, in alpha-engine-data, not here).
2. The spine/feed-reading REFRESH endpoints (price refresh / build-history / calendar
   refresh) are feed-gated, so a beta (feed-off) deployment values holdings from BROKER
   data only and never pulls spine-sourced (ultimately yfinance-derived) data to a user.
"""

from __future__ import annotations

import pathlib
import re
import uuid

import pytest

from api.config import settings

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_IMPORT_YF = re.compile(r"^\s*(import\s+yfinance|from\s+yfinance\b)", re.MULTILINE)


def test_app_code_never_imports_yfinance():
    """Locks the pyproject invariant: no metron source file imports yfinance. (If this
    fails, a market-data source was added that bypasses the data-spine.)"""
    offenders = []
    for pkg in ("api", "portfolio_analytics"):
        for path in (_REPO_ROOT / pkg).rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            if _IMPORT_YF.search(path.read_text(encoding="utf-8")):
                offenders.append(str(path.relative_to(_REPO_ROOT)))
    assert offenders == [], f"yfinance imported in: {offenders}"


@pytest.fixture()
def tenant() -> str:
    return str(uuid.uuid4())


def _hdr(t: str) -> dict:
    return {"X-Tenant-Id": t}


def _portfolio(client, tenant: str) -> str:
    return client.post("/portfolios", json={"name": "P"}, headers=_hdr(tenant)).json()["id"]


_SPINE_ENDPOINTS = ["prices/refresh", "performance/reconstruct", "calendar/refresh"]


@pytest.mark.parametrize("path", _SPINE_ENDPOINTS)
def test_beta_feed_off_blocks_spine_refresh(client, tenant, monkeypatch, path):
    """Feed off (the beta) → the spine-reading refresh endpoints 403 (no yfinance-derived
    pull); the beta values holdings from broker data only."""
    monkeypatch.setattr(settings, "tier_simulator", False)
    monkeypatch.setattr(settings, "feed_entitled", False)
    pid = _portfolio(client, tenant)
    r = client.post(f"/portfolios/{pid}/{path}", headers=_hdr(tenant))
    assert r.status_code == 403
    assert "broker" in r.json()["detail"].lower()


@pytest.mark.parametrize("path", _SPINE_ENDPOINTS)
def test_feed_on_allows_refresh(client, tenant, monkeypatch, path):
    """Feed on (owner/Pro) → the refresh endpoints are NOT gated (they run; without S3 they
    just return empty, but never 403)."""
    monkeypatch.setattr(settings, "tier_simulator", False)
    monkeypatch.setattr(settings, "feed_entitled", True)
    pid = _portfolio(client, tenant)
    r = client.post(f"/portfolios/{pid}/{path}", headers=_hdr(tenant))
    assert r.status_code == 200


@pytest.mark.parametrize("path", _SPINE_ENDPOINTS)
def test_simulator_preview_feed_off_gates(client, tenant, monkeypatch, path):
    """The owner can preview the beta's gating: simulator on + X-Preview-Feed=false → 403,
    even though the deployment's feed is on."""
    monkeypatch.setattr(settings, "tier_simulator", True)
    monkeypatch.setattr(settings, "feed_entitled", True)
    pid = _portfolio(client, tenant)
    r = client.post(f"/portfolios/{pid}/{path}", headers={**_hdr(tenant), "X-Preview-Feed": "false"})
    assert r.status_code == 403
