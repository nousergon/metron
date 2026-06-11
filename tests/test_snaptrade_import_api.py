"""Server-side SnapTrade sync endpoint — personal/single-operator, flag-gated.

Reuses the connector's recorded fake reader (tests/test_connectors_snaptrade) so the
real SnapTradeConnector builds the snapshot; only the credential source + the
deployment flag are mocked. The endpoint must 404 when the personal flag is off (so a
multi-tenant deploy can never expose the shared operator connection)."""

from __future__ import annotations

import uuid

import pytest

from tests.test_connectors_snaptrade import _FakeReader


def _hdr(tenant):
    return {"X-Tenant-Id": tenant}


def _new_portfolio(client, tenant):
    return client.post("/portfolios", json={"name": "P"}, headers=_hdr(tenant)).json()["id"]


@pytest.fixture()
def personal_on(monkeypatch):
    monkeypatch.setattr("api.routers.portfolios.settings.snaptrade_personal", True)


def _patch_from_env(monkeypatch, reader_factory):
    monkeypatch.setattr("api.routers.portfolios.SnapTradeReader.from_env", staticmethod(reader_factory))


def test_disabled_by_default_returns_404(client):
    t = str(uuid.uuid4())
    pid = _new_portfolio(client, t)
    # Flag off (default) → even a valid owner gets 404 (surface not enabled).
    r = client.post(f"/portfolios/{pid}/import/snaptrade", headers=_hdr(t))
    assert r.status_code == 404
    assert "not enabled" in r.json()["detail"]


def test_sync_persists_positions_when_enabled(client, personal_on, monkeypatch):
    _patch_from_env(monkeypatch, _FakeReader)
    t = str(uuid.uuid4())
    pid = _new_portfolio(client, t)
    r = client.post(f"/portfolios/{pid}/import/snaptrade", headers=_hdr(t))
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "snaptrade"
    assert body["positions_imported"] == 3  # AAPL, RKLB, VOO from the fixture
    holdings = {h["ticker"] for h in client.get(f"/portfolios/{pid}/holdings", headers=_hdr(t)).json()}
    assert holdings == {"AAPL", "RKLB", "VOO"}


def test_missing_credentials_returns_503(client, personal_on, monkeypatch):
    def _raise():
        raise KeyError("SNAPTRADE_CLIENT_ID")

    _patch_from_env(monkeypatch, _raise)
    t = str(uuid.uuid4())
    pid = _new_portfolio(client, t)
    r = client.post(f"/portfolios/{pid}/import/snaptrade", headers=_hdr(t))
    assert r.status_code == 503
    assert "SNAPTRADE_CLIENT_ID" in r.json()["detail"]


def test_snaptrade_failure_returns_502(client, personal_on, monkeypatch):
    class _Boom:
        def get_accounts(self):
            raise RuntimeError("token expired")

    _patch_from_env(monkeypatch, _Boom)
    t = str(uuid.uuid4())
    pid = _new_portfolio(client, t)
    r = client.post(f"/portfolios/{pid}/import/snaptrade", headers=_hdr(t))
    assert r.status_code == 502
    assert "token expired" in r.json()["detail"]


def test_cross_tenant_is_404(client, personal_on, monkeypatch):
    _patch_from_env(monkeypatch, _FakeReader)
    t = str(uuid.uuid4())
    pid = _new_portfolio(client, t)
    r = client.post(f"/portfolios/{pid}/import/snaptrade", headers=_hdr(str(uuid.uuid4())))
    assert r.status_code == 404


def test_institution_allowlist_keeps_only_matching(client, personal_on, monkeypatch):
    # Fixture accounts are all "Interactive Brokers" → allowlisting Fidelity keeps none
    # (the no-double-count-with-Flex guard), while allowlisting IBKR keeps all 3 positions.
    _patch_from_env(monkeypatch, _FakeReader)
    t = str(uuid.uuid4())

    monkeypatch.setattr("api.routers.portfolios.settings.snaptrade_institutions", "Fidelity")
    pid = _new_portfolio(client, t)
    assert client.post(f"/portfolios/{pid}/import/snaptrade", headers=_hdr(t)).json()["positions_imported"] == 0
    assert client.get(f"/portfolios/{pid}/holdings", headers=_hdr(t)).json() == []

    monkeypatch.setattr("api.routers.portfolios.settings.snaptrade_institutions", "Interactive Brokers")
    pid2 = _new_portfolio(client, t)
    assert client.post(f"/portfolios/{pid2}/import/snaptrade", headers=_hdr(t)).json()["positions_imported"] == 3


def test_preference_allowlist_overrides_env(client, personal_on, monkeypatch):
    # The portfolio's saved Settings allowlist wins over the deployment env default;
    # "all" disables filtering entirely. Fixture accounts are "Interactive Brokers".
    _patch_from_env(monkeypatch, _FakeReader)
    monkeypatch.setattr("api.routers.portfolios.settings.snaptrade_institutions", "Fidelity")
    t = str(uuid.uuid4())

    # Env-only: the Fidelity default drops every IBKR fixture account.
    pid = _new_portfolio(client, t)
    assert client.post(f"/portfolios/{pid}/import/snaptrade", headers=_hdr(t)).json()["positions_imported"] == 0

    # Saved preference overrides the env default.
    pid2 = _new_portfolio(client, t)
    client.put(
        f"/portfolios/{pid2}/preferences",
        json={"snaptrade_institutions": "Interactive Brokers"},
        headers=_hdr(t),
    )
    assert client.post(f"/portfolios/{pid2}/import/snaptrade", headers=_hdr(t)).json()["positions_imported"] == 3

    # "all" = import every linked account regardless of the env default.
    pid3 = _new_portfolio(client, t)
    client.put(f"/portfolios/{pid3}/preferences", json={"snaptrade_institutions": "all"}, headers=_hdr(t))
    assert client.post(f"/portfolios/{pid3}/import/snaptrade", headers=_hdr(t)).json()["positions_imported"] == 3
