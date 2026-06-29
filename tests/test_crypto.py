"""Standalone crypto tracking (metron-ops#111).

Wallet addresses (BTC+ETH) → Metron publishes the deduped fetch universe → the producer
writes ``crypto/holdings.json`` → this layer joins balances onto the user's addresses.
Metron makes no chain calls. Tests cover validation, address CRUD + the publish it
triggers, the balance join (pending / synced / stale), and the forward value snapshot.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest

from api.db import models
from api.services import crypto, data_spine

_BTC = "bc1q9zpgru5j9q3dccf6n5xm9wglv5jh0w8r4d5xkp"
_BTC_LEGACY = "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"
_ETH = "0x52908400098527886E0F7030069857D2E4169EE7"


@pytest.fixture(autouse=True)
def _fake_s3_writes(monkeypatch):
    """Capture data-spine S3 writes so address CRUD's publish never hits the network, and
    stub the holdings reader so endpoint GETs don't reach real S3 (service tests inject their
    own ``reader=`` and bypass this)."""
    writes: list[tuple[str, dict]] = []

    def _fake_write(bucket, key, obj, s3_client=None):
        writes.append((key, obj))

    monkeypatch.setattr(data_spine, "_write_s3_json", _fake_write)
    monkeypatch.setattr(crypto, "_default_reader", lambda: None)
    return writes


def _seed_portfolio(session):
    tenant = models.Tenant(name="t")
    session.add(tenant)
    session.flush()
    pf = models.Portfolio(tenant_id=tenant.id, name="P", base_currency="USD")
    session.add(pf)
    session.commit()
    return tenant.id, pf.id


def _art(balances: list[dict], *, as_of="2026-06-29T12:00:00Z") -> dict:
    return {"schema_version": 1, "as_of_utc": as_of, "source": "test", "balances": balances}


class TestValidation:
    def test_btc_bech32_and_legacy_ok(self):
        assert crypto.normalize_address("btc", _BTC) == ("BTC", _BTC)
        assert crypto.normalize_address("BTC", _BTC_LEGACY) == ("BTC", _BTC_LEGACY)

    def test_eth_lowercased(self):
        # Mixed-case ETH input canonicalizes to lowercase so it dedupes to one fetch.
        assert crypto.normalize_address("ETH", _ETH) == ("ETH", _ETH.lower())

    def test_bad_chain_rejected(self):
        with pytest.raises(crypto.InvalidAddress):
            crypto.normalize_address("DOGE", _BTC)

    def test_bad_eth_rejected(self):
        with pytest.raises(crypto.InvalidAddress):
            crypto.normalize_address("ETH", "0x1234")  # too short

    def test_bad_btc_rejected(self):
        with pytest.raises(crypto.InvalidAddress):
            crypto.normalize_address("BTC", "not-an-address")


class TestAddressCrud:
    def test_add_dedupes_and_updates_label(self, db_session):
        tid, pid = _seed_portfolio(db_session)
        crypto.add_address(db_session, tid, pid, "ETH", _ETH, label="cold")
        # Re-add same wallet (different case) → one row, label updated.
        crypto.add_address(db_session, tid, pid, "ETH", _ETH.lower(), label="hot")
        rows = crypto.list_addresses(db_session, tid, pid)
        assert len(rows) == 1 and rows[0].label == "hot" and rows[0].address == _ETH.lower()

    def test_delete_scoped_to_portfolio(self, db_session):
        tid, pid = _seed_portfolio(db_session)
        row = crypto.add_address(db_session, tid, pid, "BTC", _BTC)
        assert crypto.delete_address(db_session, tid, pid, uuid.uuid4()) is False  # wrong id
        assert crypto.delete_address(db_session, tid, pid, row.id) is True
        assert crypto.list_addresses(db_session, tid, pid) == []

    def test_publish_called_on_change(self, db_session, _fake_s3_writes):
        tid, pid = _seed_portfolio(db_session)
        crypto.add_address(db_session, tid, pid, "BTC", _BTC)
        keys = [k for k, _ in _fake_s3_writes]
        assert data_spine.WALLET_ADDRESSES_KEY in keys
        payload = next(o for k, o in _fake_s3_writes if k == data_spine.WALLET_ADDRESSES_KEY)
        assert payload["addresses"] == [{"chain": "BTC", "address": _BTC}]


class TestBuildUniverse:
    def test_dedupes_across_portfolios(self, db_session):
        tid, pid = _seed_portfolio(db_session)
        _, pid2 = _seed_portfolio(db_session)  # different tenant/portfolio, same address
        db_session.add_all([
            models.WalletAddress(tenant_id=tid, portfolio_id=pid, chain="BTC", address=_BTC),
        ])
        db_session.commit()
        uni = data_spine.build_wallet_addresses(db_session)
        assert uni["addresses"] == [{"chain": "BTC", "address": _BTC}]


class TestForPortfolio:
    def test_pending_when_no_artifact(self, db_session):
        tid, pid = _seed_portfolio(db_session)
        crypto.add_address(db_session, tid, pid, "BTC", _BTC)
        s = crypto.for_portfolio(db_session, tid, pid, reader=lambda: None)
        assert s.available is False and s.reason == "unavailable"
        assert s.n_pending == 1 and s.positions[0].synced is False and s.total_usd is None

    def test_synced_join_and_total(self, db_session):
        tid, pid = _seed_portfolio(db_session)
        crypto.add_address(db_session, tid, pid, "BTC", _BTC, label="cold")
        crypto.add_address(db_session, tid, pid, "ETH", _ETH)
        art = _art([
            {"chain": "BTC", "address": _BTC, "symbol": "BTC", "balance": 0.5, "price_usd": 60000.0, "value_usd": 30000.0},
            {"chain": "ETH", "address": _ETH.lower(), "symbol": "ETH", "balance": 2.0, "price_usd": 3000.0},
        ])
        now = datetime(2026, 6, 29, 12, 5, tzinfo=UTC)
        s = crypto.for_portfolio(db_session, tid, pid, reader=lambda: art, now=now)
        assert s.available is True and s.n_pending == 0
        # value_usd computed from balance×price when the producer omits it (ETH row).
        assert s.total_usd == pytest.approx(30000.0 + 6000.0)
        btc = next(p for p in s.positions if p.chain == "BTC")
        assert btc.synced and btc.value_usd == 30000.0 and btc.label == "cold"

    def test_stale_artifact_marks_pending(self, db_session):
        tid, pid = _seed_portfolio(db_session)
        crypto.add_address(db_session, tid, pid, "BTC", _BTC)
        art = _art([{"chain": "BTC", "address": _BTC, "balance": 1.0, "price_usd": 1.0, "value_usd": 1.0}],
                   as_of="2026-06-29T08:00:00Z")
        now = datetime(2026, 6, 29, 12, 0, tzinfo=UTC)  # 4h later → stale (>1h)
        s = crypto.for_portfolio(db_session, tid, pid, reader=lambda: art, now=now)
        assert s.stale is True and s.available is False and s.positions[0].synced is False


class TestSnapshot:
    def test_records_and_is_idempotent(self, db_session):
        tid, pid = _seed_portfolio(db_session)
        crypto.add_address(db_session, tid, pid, "BTC", _BTC)
        art = _art([{"chain": "BTC", "address": _BTC, "symbol": "BTC", "balance": 1.0, "price_usd": 50000.0, "value_usd": 50000.0}])
        now = datetime(2026, 6, 29, 12, 5, tzinfo=UTC)
        s = crypto.for_portfolio(db_session, tid, pid, reader=lambda: art, now=now)
        crypto.record_snapshot(db_session, tid, pid, s, today=date(2026, 6, 29))
        crypto.record_snapshot(db_session, tid, pid, s, today=date(2026, 6, 29))  # idempotent
        rows = db_session.query(models.CryptoValueSnapshot).all()
        assert len(rows) == 1 and float(rows[0].value_usd) == 50000.0

    def test_skipped_without_total(self, db_session):
        tid, pid = _seed_portfolio(db_session)
        crypto.add_address(db_session, tid, pid, "BTC", _BTC)
        s = crypto.for_portfolio(db_session, tid, pid, reader=lambda: None)
        assert crypto.record_snapshot(db_session, tid, pid, s, today=date(2026, 6, 29)) is None
        assert db_session.query(models.CryptoValueSnapshot).count() == 0


@pytest.fixture()
def tenant():
    return str(uuid.uuid4())


class TestCryptoEndpoints:
    def test_add_validate_list_delete(self, client, tenant):
        pid = client.post("/portfolios", json={"name": "P"}, headers={"X-Tenant-Id": tenant}).json()["id"]
        # Bad address → 422.
        bad = client.post(
            f"/portfolios/{pid}/crypto/addresses",
            json={"chain": "ETH", "address": "0xnope"}, headers={"X-Tenant-Id": tenant},
        )
        assert bad.status_code == 422
        # Good address → 201, pending.
        ok = client.post(
            f"/portfolios/{pid}/crypto/addresses",
            json={"chain": "BTC", "address": _BTC, "label": "cold"}, headers={"X-Tenant-Id": tenant},
        )
        assert ok.status_code == 201 and ok.json()["synced"] is False
        # GET summary lists it as pending (no producer artifact in the test env).
        summ = client.get(f"/portfolios/{pid}/crypto", headers={"X-Tenant-Id": tenant}).json()
        assert summ["n_pending"] == 1 and len(summ["positions"]) == 1
        addr_id = summ["positions"][0]["id"]
        # Delete it → 200; deleting again → 404.
        d = client.delete(f"/portfolios/{pid}/crypto/addresses/{addr_id}", headers={"X-Tenant-Id": tenant})
        assert d.status_code == 200 and d.json()["removed"] is True
        d2 = client.delete(f"/portfolios/{pid}/crypto/addresses/{addr_id}", headers={"X-Tenant-Id": tenant})
        assert d2.status_code == 404
        assert client.get(f"/portfolios/{pid}/crypto", headers={"X-Tenant-Id": tenant}).json()["positions"] == []
