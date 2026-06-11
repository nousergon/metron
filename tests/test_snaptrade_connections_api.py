"""In-product SnapTrade connection management — list connections + portal link.

Same personal-mode gating and error contract as the sync endpoint (404 flag-off /
503 unconfigured / 502 upstream failure, always with a reason). The reader is
stubbed at the same seam the sync tests use (``SnapTradeReader.from_env``)."""

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

    def get_connections(self):
        return [
            {"id": "auth-1", "brokerage": "Fidelity", "disabled": False},
            {"id": "auth-2", "brokerage": "E*Trade", "disabled": True},
        ]

    def get_accounts(self):
        return [
            {"id": "1", "number": "X1", "institution": "Fidelity", "brokerage_authorization": "auth-1"},
            {"id": "2", "number": "X2", "institution": "Fidelity", "brokerage_authorization": "auth-1"},
            {"id": "3", "number": "X3", "institution": "E*TRADE", "brokerage_authorization": "auth-2"},
        ]

    def __init__(self):
        self.removed = []
        self.login_kwargs = None

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


def test_connections_listed_with_counts_and_allowlist_marking(client, personal_on, monkeypatch):
    _patch_from_env(monkeypatch, _ConnReader)
    monkeypatch.setattr("api.routers.portfolios.settings.snaptrade_institutions", "Fidelity")
    t = str(uuid.uuid4())
    pid = _new_portfolio(client, t)
    r = client.get(f"/portfolios/{pid}/snaptrade/connections", headers=_hdr(t))
    assert r.status_code == 200
    body = r.json()
    assert body["allowlist"] == ["Fidelity"]
    by_brokerage = {c["brokerage"]: c for c in body["connections"]}
    assert by_brokerage["Fidelity"] == {
        "id": "auth-1",
        "brokerage": "Fidelity",
        "disabled": False,
        "n_accounts": 2,
        "allowed": True,
    }
    # E*TRADE accounts don't clear the Fidelity-only allowlist → marked filtered.
    assert by_brokerage["E*Trade"]["disabled"] is True
    assert by_brokerage["E*Trade"]["n_accounts"] == 1
    assert by_brokerage["E*Trade"]["allowed"] is False


def test_connections_preference_allowlist_overrides_env(client, personal_on, monkeypatch):
    _patch_from_env(monkeypatch, _ConnReader)
    monkeypatch.setattr("api.routers.portfolios.settings.snaptrade_institutions", "Fidelity")
    t = str(uuid.uuid4())
    pid = _new_portfolio(client, t)
    client.put(
        f"/portfolios/{pid}/preferences",
        json={"snaptrade_institutions": "E*Trade"},
        headers=_hdr(t),
    )
    body = client.get(f"/portfolios/{pid}/snaptrade/connections", headers=_hdr(t)).json()
    assert body["allowlist"] == ["E*Trade"]
    by_brokerage = {c["brokerage"]: c for c in body["connections"]}
    assert by_brokerage["Fidelity"]["allowed"] is False
    assert by_brokerage["E*Trade"]["allowed"] is True

    # "all" disables filtering entirely.
    client.put(f"/portfolios/{pid}/preferences", json={"snaptrade_institutions": "all"}, headers=_hdr(t))
    body = client.get(f"/portfolios/{pid}/snaptrade/connections", headers=_hdr(t)).json()
    assert body["allowlist"] == []
    assert all(c["allowed"] for c in body["connections"])


def test_connect_returns_portal_url(client, personal_on, monkeypatch):
    _patch_from_env(monkeypatch, _ConnReader)
    t = str(uuid.uuid4())
    pid = _new_portfolio(client, t)
    r = client.post(f"/portfolios/{pid}/snaptrade/connect", headers=_hdr(t))
    assert r.status_code == 200
    assert r.json() == {"redirect_uri": "https://app.snaptrade.com/connect?token=abc"}


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
    assert (
        client.get(f"/portfolios/{pid}/snaptrade/connections", headers=_hdr(str(uuid.uuid4()))).status_code
        == 404
    )


def test_connect_reconnect_passes_authorization_id(client, personal_on, monkeypatch):
    reader = _ConnReader()
    _patch_from_env(monkeypatch, lambda: reader)
    t = str(uuid.uuid4())
    pid = _new_portfolio(client, t)
    r = client.post(f"/portfolios/{pid}/snaptrade/connect", json={"reconnect": "auth-2"}, headers=_hdr(t))
    assert r.status_code == 200
    assert reader.login_kwargs == {"broker": None, "reconnect": "auth-2"}


def test_remove_connection_deletes_at_snaptrade(client, personal_on, monkeypatch):
    reader = _ConnReader()
    _patch_from_env(monkeypatch, lambda: reader)
    t = str(uuid.uuid4())
    pid = _new_portfolio(client, t)
    r = client.delete(f"/portfolios/{pid}/snaptrade/connections/auth-2", headers=_hdr(t))
    assert r.status_code == 200
    assert r.json() == {"removed": "auth-2"}
    assert reader.removed == ["auth-2"]


def test_remove_connection_disabled_by_default_404(client):
    t = str(uuid.uuid4())
    pid = _new_portfolio(client, t)
    assert client.delete(f"/portfolios/{pid}/snaptrade/connections/auth-1", headers=_hdr(t)).status_code == 404


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


def test_remove_connection_cross_tenant_404(client, personal_on, monkeypatch):
    _patch_from_env(monkeypatch, _ConnReader)
    t = str(uuid.uuid4())
    pid = _new_portfolio(client, t)
    r = client.delete(f"/portfolios/{pid}/snaptrade/connections/auth-1", headers=_hdr(str(uuid.uuid4())))
    assert r.status_code == 404


def test_include_appends_actual_institution_strings_over_env_default(client, personal_on, monkeypatch):
    # The live trap this guards: accounts report "E-Trade" (hyphen) while the
    # connection's brokerage displays "E*Trade" (asterisk) — include must persist
    # the ACCOUNT string, materializing the env default into the preference first.
    class _ETradeReader(_ConnReader):
        def get_accounts(self):
            return [
                {"id": "1", "number": "X1", "institution": "Fidelity", "brokerage_authorization": "auth-1"},
                {"id": "3", "number": "X3", "institution": "E-Trade", "brokerage_authorization": "auth-2"},
            ]

    _patch_from_env(monkeypatch, _ETradeReader)
    monkeypatch.setattr("api.routers.portfolios.settings.snaptrade_institutions", "Fidelity")
    t = str(uuid.uuid4())
    pid = _new_portfolio(client, t)
    r = client.post(f"/portfolios/{pid}/snaptrade/connections/auth-2/include", headers=_hdr(t))
    assert r.status_code == 200
    assert r.json() == {"added": ["E-Trade"], "allowlist": ["Fidelity", "E-Trade"]}
    # Persisted to the Settings preference (env default materialized + appended).
    prefs = client.get(f"/portfolios/{pid}/preferences", headers=_hdr(t)).json()
    assert prefs["snaptrade_institutions"] == "Fidelity, E-Trade"
    # The connection now clears the allowlist.
    conns = client.get(f"/portfolios/{pid}/snaptrade/connections", headers=_hdr(t)).json()
    assert all(c["allowed"] for c in conns["connections"])
    # Idempotent — a second include adds nothing.
    again = client.post(f"/portfolios/{pid}/snaptrade/connections/auth-2/include", headers=_hdr(t))
    assert again.json() == {"added": [], "allowlist": ["Fidelity", "E-Trade"]}


def test_include_noop_when_allowlist_empty(client, personal_on, monkeypatch):
    # Empty effective allowlist = everything imports; include has nothing to add.
    _patch_from_env(monkeypatch, _ConnReader)
    monkeypatch.setattr("api.routers.portfolios.settings.snaptrade_institutions", "")
    t = str(uuid.uuid4())
    pid = _new_portfolio(client, t)
    r = client.post(f"/portfolios/{pid}/snaptrade/connections/auth-2/include", headers=_hdr(t))
    assert r.status_code == 200
    assert r.json() == {"added": [], "allowlist": []}


def test_include_unknown_connection_404_with_reason(client, personal_on, monkeypatch):
    _patch_from_env(monkeypatch, _ConnReader)
    t = str(uuid.uuid4())
    pid = _new_portfolio(client, t)
    r = client.post(f"/portfolios/{pid}/snaptrade/connections/auth-9/include", headers=_hdr(t))
    assert r.status_code == 404
    assert "No accounts found" in r.json()["detail"]


def test_include_disabled_by_default_404(client):
    t = str(uuid.uuid4())
    pid = _new_portfolio(client, t)
    assert client.post(f"/portfolios/{pid}/snaptrade/connections/auth-1/include", headers=_hdr(t)).status_code == 404
