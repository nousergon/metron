"""In-product SnapTrade connection management — list, portal link, remove, and the
per-connection sync exclusion toggle.

Linked = synced by default; exclusion is keyed by the connection's stable
authorization id (never institution-name matching, which proved fragile — SnapTrade
reports "E-Trade" on accounts but "E*Trade" on the connection). Same personal-mode
gating and error contract as the sync endpoint (404 flag-off / 503 unconfigured /
502 upstream failure, always with a reason)."""

from __future__ import annotations

import uuid

import pytest


def _hdr(tenant):
    return {"X-Tenant-Id": tenant}


def _new_portfolio(client, tenant):
    return client.post("/portfolios", json={"name": "P"}, headers=_hdr(tenant)).json()["id"]


@pytest.fixture()
def personal_on(monkeypatch):
    monkeypatch.setattr("api.routers.portfolios.settings.snaptrade_personal", True)


def _patch_from_env(monkeypatch, reader_factory):
    monkeypatch.setattr("api.routers.portfolios.SnapTradeReader.from_env", staticmethod(reader_factory))


class _ConnReader:
    """Two connections: Fidelity (2 accounts) + a disabled E*TRADE (1 account)."""

    def __init__(self):
        self.removed = []
        self.login_kwargs = None

    def get_connections(self):
        return [
            {"id": "auth-1", "brokerage": "Fidelity", "disabled": False},
            {"id": "auth-2", "brokerage": "E*Trade", "disabled": True},
        ]

    def get_accounts(self):
        return [
            {"id": "1", "number": "X1", "institution": "Fidelity", "brokerage_authorization": "auth-1"},
            {"id": "2", "number": "X2", "institution": "Fidelity", "brokerage_authorization": "auth-1"},
            # Account institution differs from the connection's display name (the
            # live E-Trade-vs-E*Trade case) — irrelevant now: exclusion is by id.
            {"id": "3", "number": "X3", "institution": "E-Trade", "brokerage_authorization": "auth-2"},
        ]

    def get_login_url(self, broker=None, reconnect=None):
        self.login_kwargs = {"broker": broker, "reconnect": reconnect}
        return "https://app.snaptrade.com/connect?token=abc"

    def remove_connection(self, authorization_id):
        self.removed.append(authorization_id)


def test_connections_disabled_by_default_returns_404(client):
    t = str(uuid.uuid4())
    pid = _new_portfolio(client, t)
    assert client.get(f"/portfolios/{pid}/snaptrade/connections", headers=_hdr(t)).status_code == 404
    assert client.post(f"/portfolios/{pid}/snaptrade/connect", headers=_hdr(t)).status_code == 404
    assert client.post(f"/portfolios/{pid}/snaptrade/connections/auth-1/exclude", headers=_hdr(t)).status_code == 404
    assert client.delete(f"/portfolios/{pid}/snaptrade/connections/auth-1", headers=_hdr(t)).status_code == 404


def test_connections_listed_with_counts_default_included(client, personal_on, monkeypatch):
    _patch_from_env(monkeypatch, _ConnReader)
    t = str(uuid.uuid4())
    pid = _new_portfolio(client, t)
    r = client.get(f"/portfolios/{pid}/snaptrade/connections", headers=_hdr(t))
    assert r.status_code == 200
    by_brokerage = {c["brokerage"]: c for c in r.json()["connections"]}
    assert by_brokerage["Fidelity"] == {
        "id": "auth-1",
        "brokerage": "Fidelity",
        "disabled": False,
        "n_accounts": 2,
        "excluded": False,
    }
    # Linked = synced by default — even though the account institution string
    # ("E-Trade") differs from the connection name ("E*Trade").
    assert by_brokerage["E*Trade"]["excluded"] is False
    assert by_brokerage["E*Trade"]["n_accounts"] == 1
    # Nothing imported yet → "linked but never synced" signal (metron-ops#21).
    assert r.json()["n_synced_accounts"] == 0


def test_exclude_include_toggle_persists(client, personal_on, monkeypatch):
    _patch_from_env(monkeypatch, _ConnReader)
    t = str(uuid.uuid4())
    pid = _new_portfolio(client, t)
    r = client.post(f"/portfolios/{pid}/snaptrade/connections/auth-1/exclude", headers=_hdr(t))
    assert r.status_code == 200
    assert r.json() == {"id": "auth-1", "excluded": True}
    conns = {
        c["id"]: c
        for c in client.get(f"/portfolios/{pid}/snaptrade/connections", headers=_hdr(t)).json()["connections"]
    }
    assert conns["auth-1"]["excluded"] is True
    assert conns["auth-2"]["excluded"] is False
    # Idempotent.
    assert client.post(f"/portfolios/{pid}/snaptrade/connections/auth-1/exclude", headers=_hdr(t)).status_code == 200
    # Include undoes it.
    r = client.post(f"/portfolios/{pid}/snaptrade/connections/auth-1/include", headers=_hdr(t))
    assert r.json() == {"id": "auth-1", "excluded": False}
    conns = {
        c["id"]: c
        for c in client.get(f"/portfolios/{pid}/snaptrade/connections", headers=_hdr(t)).json()["connections"]
    }
    assert conns["auth-1"]["excluded"] is False


def test_exclusion_is_per_portfolio(client, personal_on, monkeypatch):
    _patch_from_env(monkeypatch, _ConnReader)
    t = str(uuid.uuid4())
    pid1 = _new_portfolio(client, t)
    pid2 = _new_portfolio(client, t)
    client.post(f"/portfolios/{pid1}/snaptrade/connections/auth-2/exclude", headers=_hdr(t))
    c1 = {
        c["id"]: c
        for c in client.get(f"/portfolios/{pid1}/snaptrade/connections", headers=_hdr(t)).json()["connections"]
    }
    c2 = {
        c["id"]: c
        for c in client.get(f"/portfolios/{pid2}/snaptrade/connections", headers=_hdr(t)).json()["connections"]
    }
    assert c1["auth-2"]["excluded"] is True
    assert c2["auth-2"]["excluded"] is False


def test_remove_connection_deletes_and_prunes_exclusion(client, personal_on, monkeypatch):
    reader = _ConnReader()
    _patch_from_env(monkeypatch, lambda: reader)
    t = str(uuid.uuid4())
    pid = _new_portfolio(client, t)
    client.post(f"/portfolios/{pid}/snaptrade/connections/auth-2/exclude", headers=_hdr(t))
    r = client.delete(f"/portfolios/{pid}/snaptrade/connections/auth-2", headers=_hdr(t))
    assert r.status_code == 200
    assert r.json() == {"removed": "auth-2"}
    assert reader.removed == ["auth-2"]
    # The stale id can't linger in the exclusion set (a re-link gets a new id).
    conns = {
        c["id"]: c
        for c in client.get(f"/portfolios/{pid}/snaptrade/connections", headers=_hdr(t)).json()["connections"]
    }
    assert conns["auth-2"]["excluded"] is False


def test_connect_returns_portal_url(client, personal_on, monkeypatch):
    _patch_from_env(monkeypatch, _ConnReader)
    t = str(uuid.uuid4())
    pid = _new_portfolio(client, t)
    r = client.post(f"/portfolios/{pid}/snaptrade/connect", headers=_hdr(t))
    assert r.status_code == 200
    assert r.json() == {"redirect_uri": "https://app.snaptrade.com/connect?token=abc"}


def test_connect_reconnect_passes_authorization_id(client, personal_on, monkeypatch):
    reader = _ConnReader()
    _patch_from_env(monkeypatch, lambda: reader)
    t = str(uuid.uuid4())
    pid = _new_portfolio(client, t)
    r = client.post(f"/portfolios/{pid}/snaptrade/connect", json={"reconnect": "auth-2"}, headers=_hdr(t))
    assert r.status_code == 200
    assert reader.login_kwargs == {"broker": None, "reconnect": "auth-2"}


def test_connect_upstream_failure_returns_502_with_reason(client, personal_on, monkeypatch):
    class _Boom:
        def get_login_url(self, broker=None, reconnect=None):
            raise RuntimeError("portal unavailable")

    _patch_from_env(monkeypatch, _Boom)
    t = str(uuid.uuid4())
    pid = _new_portfolio(client, t)
    r = client.post(f"/portfolios/{pid}/snaptrade/connect", headers=_hdr(t))
    assert r.status_code == 502
    assert "portal unavailable" in r.json()["detail"]


def test_remove_connection_upstream_failure_502_with_reason(client, personal_on, monkeypatch):
    class _Boom:
        def remove_connection(self, authorization_id):
            raise RuntimeError("authorization not found")

    _patch_from_env(monkeypatch, _Boom)
    t = str(uuid.uuid4())
    pid = _new_portfolio(client, t)
    r = client.delete(f"/portfolios/{pid}/snaptrade/connections/auth-9", headers=_hdr(t))
    assert r.status_code == 502
    assert "authorization not found" in r.json()["detail"]


def test_connections_missing_credentials_returns_503(client, personal_on, monkeypatch):
    def _raise():
        raise KeyError("SNAPTRADE_USER_SECRET")

    _patch_from_env(monkeypatch, _raise)
    t = str(uuid.uuid4())
    pid = _new_portfolio(client, t)
    r = client.get(f"/portfolios/{pid}/snaptrade/connections", headers=_hdr(t))
    assert r.status_code == 503
    assert "SNAPTRADE_USER_SECRET" in r.json()["detail"]


def test_connections_cross_tenant_is_404(client, personal_on, monkeypatch):
    _patch_from_env(monkeypatch, _ConnReader)
    t = str(uuid.uuid4())
    pid = _new_portfolio(client, t)
    foreign = _hdr(str(uuid.uuid4()))
    assert client.get(f"/portfolios/{pid}/snaptrade/connections", headers=foreign).status_code == 404
    assert client.post(f"/portfolios/{pid}/snaptrade/connections/auth-1/exclude", headers=foreign).status_code == 404
    assert client.delete(f"/portfolios/{pid}/snaptrade/connections/auth-1", headers=foreign).status_code == 404
