"""Unit tests for the connector + canonical ingestion layer (PR1).

Covers the canonical schema helpers, the silver store's per-record merge semantics
(replace vs upsert vs union), bronze landing + failure handling, store round-trip +
malformed-row tolerance, and the ingest ownership policy across the three cutover
modes plus per-connector degradation. Pure, no network — mirrors the inline-fixture
style of test_ibkr_flex.py (no conftest).
"""

from __future__ import annotations

from datetime import date, datetime

from portfolio_analytics.domain.ledger import RealizedGain, TxnType
from portfolio_analytics.ingestion.base import BrokerConnector, ConnectorSnapshot
from portfolio_analytics.ingestion.ingest import OwnershipPolicy, _filter_owned, ingest
from portfolio_analytics.ingestion.schema import (
    CanonicalAccount,
    CanonicalActivity,
    CanonicalHolding,
    CanonicalSecurity,
    activity_key,
    lot_key,
    synth_security_id,
)
from portfolio_analytics.ingestion.store import (
    CanonicalStore,
    load_store,
    save_bronze,
    save_store,
)

_AS_OF = datetime(2026, 6, 8, 12, 0, 0)


# ── builders ────────────────────────────────────────────────────────────────
def _acct(number, source="snaptrade", nav=1000.0, **kw):
    return CanonicalAccount(number=number, nav_usd=nav, as_of=_AS_OF, source=source, **kw)


def _sec(ticker, currency="USD"):
    sid = synth_security_id(ticker, currency)
    return CanonicalSecurity(security_id=sid, ticker=ticker, currency=currency)


def _hold(number, ticker, qty=10.0, source="snaptrade", currency="USD"):
    return CanonicalHolding(
        account_number=number,
        security_id=synth_security_id(ticker, currency),
        quantity=qty,
        avg_cost=5.0,
        market_value_local=qty * 6.0,
        currency=currency,
        as_of=_AS_OF,
        source=source,
    )


def _act(number, ticker, when=date(2026, 1, 2), ttype=TxnType.DIVIDEND, amount=12.0, source="snaptrade"):
    return CanonicalActivity(
        account_number=number,
        when=when,
        type=ttype,
        security_id=synth_security_id(ticker) if ticker else "",
        amount=amount,
        as_of=_AS_OF,
        source=source,
    )


def _lot(number, ticker, gain=100.0):
    rg = RealizedGain(
        ticker=ticker,
        open_date=date(2025, 1, 1),
        close_date=date(2026, 1, 1),
        quantity=10.0,
        proceeds=200.0 + gain,
        cost_basis=200.0,
    )
    return (number, rg)


class _FakeConnector:
    """Minimal BrokerConnector for ingest tests."""

    def __init__(self, source, snapshot=None, raises=False):
        self.source = source
        self._snapshot = snapshot if snapshot is not None else ConnectorSnapshot(source=source)
        self._raises = raises

    def sync(self, state=None):
        if self._raises:
            raise RuntimeError("boom")
        return self._snapshot


# ── schema ──────────────────────────────────────────────────────────────────
def test_synth_security_id_disambiguates_currency():
    assert synth_security_id("rklb") == "EQ:RKLB:USD"
    assert synth_security_id("0700", "HKD") != synth_security_id("0700", "USD")


def test_activity_and_lot_keys_are_stable():
    a = _act("U1", "AAPL")
    assert activity_key(a) == activity_key(_act("U1", "AAPL"))
    number, rg = _lot("U1", "AAPL")
    assert lot_key(number, rg) == lot_key(number, rg)


def test_fakeconnector_satisfies_protocol():
    assert isinstance(_FakeConnector("x"), BrokerConnector)


# ── store merge semantics ─────────────────────────────────────────────────────
def test_accounts_last_write_wins():
    s = CanonicalStore()
    s.merge("snaptrade", [_acct("U1", nav=1000.0)], [], [], [], [])
    s.merge("snaptrade", [_acct("U1", nav=2000.0)], [], [], [], [])
    assert len(s.all_accounts()) == 1
    assert s.accounts["U1"].nav_usd == 2000.0


def test_holdings_replaced_per_account_clears_stale():
    s = CanonicalStore()
    s.merge(
        "snaptrade", [_acct("U1")], [_sec("AAPL"), _sec("MSFT")], [_hold("U1", "AAPL"), _hold("U1", "MSFT")], [], []
    )
    assert len(s.holdings["U1"]) == 2
    # second sync: only AAPL remains (MSFT sold) — replace must drop MSFT
    s.merge("snaptrade", [_acct("U1")], [_sec("AAPL")], [_hold("U1", "AAPL")], [], [])
    assert [h.security_id for h in s.holdings["U1"]] == [synth_security_id("AAPL")]


def test_account_with_no_holdings_is_cleared():
    s = CanonicalStore()
    s.merge("snaptrade", [_acct("U1")], [_sec("AAPL")], [_hold("U1", "AAPL")], [], [])
    s.merge("snaptrade", [_acct("U1")], [], [], [], [])  # all positions closed
    assert s.holdings["U1"] == []


def test_securities_upsert_master():
    s = CanonicalStore()
    s.merge("snaptrade", [_acct("U1")], [_sec("AAPL")], [], [], [])
    s.merge("flex", [_acct("U2", source="flex")], [_sec("AAPL"), _sec("RKLB")], [], [], [])
    assert set(s.securities) == {synth_security_id("AAPL"), synth_security_id("RKLB")}


def test_activities_and_lots_union_append():
    s = CanonicalStore()
    s.merge("snaptrade", [_acct("U1")], [], [], [_act("U1", "AAPL")], [_lot("U1", "AAPL")])
    s.merge(
        "snaptrade",
        [_acct("U1")],
        [],
        [],
        [_act("U1", "AAPL"), _act("U1", "MSFT")],
        [_lot("U1", "AAPL"), _lot("U1", "MSFT")],
    )
    assert len(s.all_activities()) == 2  # AAPL deduped, MSFT added
    assert len(s.all_realized_lots()) == 2


# ── store persistence ─────────────────────────────────────────────────────────
def test_store_round_trip(tmp_path):
    path = tmp_path / "silver.json"
    s = CanonicalStore()
    s.merge(
        "snaptrade",
        [_acct("U1", nav=12345.67, label="Growth", tax_treatment="taxable")],
        [_sec("AAPL")],
        [_hold("U1", "AAPL")],
        [_act("U1", "AAPL")],
        [_lot("U1", "AAPL")],
    )
    save_store(s, path)
    loaded = load_store(path)
    assert loaded.accounts["U1"].nav_usd == 12345.67
    assert loaded.accounts["U1"].as_of == _AS_OF
    assert len(loaded.all_holdings()) == 1
    assert loaded.all_activities()[0].type == TxnType.DIVIDEND
    assert loaded.all_realized_lots()[0][1].ticker == "AAPL"
    assert loaded.security(synth_security_id("AAPL")).ticker == "AAPL"


def test_load_missing_store_is_empty(tmp_path):
    assert load_store(tmp_path / "nope.json").all_accounts() == []


def test_load_corrupt_store_returns_empty(tmp_path):
    p = tmp_path / "silver.json"
    p.write_text("{not json")
    assert load_store(p).all_accounts() == []


def test_load_skips_malformed_rows(tmp_path):
    p = tmp_path / "silver.json"
    p.write_text(
        '{"accounts":[{"number":"U1","nav_usd":5.0},{"label":"no-number"}],'
        '"holdings":[{"account_number":"U1"}],'  # missing security_id → skipped
        '"activities":[{"account_number":"U1","when":"2026-01-02","type":"DIVIDEND","amount":1.0}],'
        '"realized_lots":[{"bad":"row"}]}'
    )
    s = load_store(p)
    assert list(s.accounts) == ["U1"]  # the no-number row skipped
    assert s.all_holdings() == []  # malformed holding skipped
    assert len(s.all_activities()) == 1
    assert s.all_realized_lots() == []  # malformed lot skipped


# ── bronze ────────────────────────────────────────────────────────────────────
def test_save_bronze_lands_payload_and_manifest(tmp_path):
    entry = save_bronze("flex", "<xml>raw</xml>", bronze_dir=tmp_path, fetched_at=_AS_OF)
    assert entry is not None and entry["bytes"] == len("<xml>raw</xml>")
    assert (tmp_path / "flex" / "latest.raw").read_text() == "<xml>raw</xml>"
    assert (tmp_path / "flex" / "_manifest.ndjson").exists()


def test_save_bronze_failure_is_caught(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a dir")
    # bronze_dir/<source> can't be created under a regular file → OSError, caught → None
    assert save_bronze("flex", b"data", bronze_dir=blocker) is None


# ── ingest ownership + degradation ────────────────────────────────────────────
def _snap(source, accounts, holdings=(), activities=(), lots=(), securities=()):
    return ConnectorSnapshot(
        source=source,
        accounts=list(accounts),
        securities=list(securities),
        holdings=list(holdings),
        activities=list(activities),
        realized_lots=list(lots),
    )


def test_ownership_snaptrade_mode_default_owns_everything():
    # default_source owns the IBKR account; SnapTrade records kept.
    snap = _FakeConnector(
        "snaptrade", _snap("snaptrade", [_acct("U_IBKR")], [_hold("U_IBKR", "RKLB")], securities=[_sec("RKLB")])
    )
    policy = OwnershipPolicy(explicit={}, default_source="snaptrade")
    store = ingest([snap], policy, store=CanonicalStore(), persist=False)
    assert store.holdings["U_IBKR"]


def test_ownership_flex_mode_suppresses_snaptrade_ibkr_no_double_count():
    ibkr = "U_IBKR"
    st = _FakeConnector(
        "snaptrade",
        _snap(
            "snaptrade",
            [_acct(ibkr, source="snaptrade"), _acct("U_FID", source="snaptrade")],
            [_hold(ibkr, "RKLB", source="snaptrade"), _hold("U_FID", "VOO", source="snaptrade")],
            securities=[_sec("RKLB"), _sec("VOO")],
        ),
    )
    fx = _FakeConnector(
        "flex",
        _snap(
            "flex",
            [_acct(ibkr, source="flex", nav=9999.0)],
            [_hold(ibkr, "RKLB", qty=42.0, source="flex")],
            securities=[_sec("RKLB")],
        ),
    )
    policy = OwnershipPolicy(explicit={"flex": {ibkr}}, default_source="snaptrade")
    store = ingest([st, fx], policy, store=CanonicalStore(), persist=False)
    # IBKR owned by flex → flex's record wins, SnapTrade's IBKR dropped (no double count)
    assert store.accounts[ibkr].source == "flex"
    assert store.accounts[ibkr].nav_usd == 9999.0
    assert store.holdings[ibkr][0].quantity == 42.0
    # Fidelity still served by SnapTrade
    assert store.accounts["U_FID"].source == "snaptrade"
    # exactly one IBKR account, not two
    assert sum(1 for a in store.all_accounts() if a.number == ibkr) == 1


def test_ownership_observe_mode_drops_unclaimed_flex_records():
    ibkr = "U_IBKR"
    fx = _FakeConnector(
        "flex",
        _snap(
            "flex",
            [_acct(ibkr, source="flex")],
            [_hold(ibkr, "RKLB", source="flex")],
            [_act(ibkr, "RKLB", source="flex")],
            [_lot(ibkr, "RKLB")],
            securities=[_sec("RKLB")],
        ),
    )
    # observe: flex claims nothing; default is snaptrade → flex's IBKR records all drop
    policy = OwnershipPolicy(explicit={}, default_source="snaptrade")
    store = ingest([fx], policy, store=CanonicalStore(), persist=False)
    assert store.all_accounts() == []
    assert store.all_holdings() == []
    assert store.all_activities() == []
    assert store.all_realized_lots() == []
    assert list(store.securities) == []  # unreferenced securities not upserted


def test_ownership_no_owner_fallback_drops_record():
    snap = _FakeConnector("flex", _snap("flex", [_acct("U_X", source="flex")]))
    policy = OwnershipPolicy(explicit={}, default_source=None)  # nobody owns U_X
    store = ingest([snap], policy, store=CanonicalStore(), persist=False)
    assert store.all_accounts() == []


def test_filter_owned_keeps_only_referenced_securities():
    snap = _snap(
        "flex",
        [_acct("U1", source="flex")],
        [_hold("U1", "AAPL", source="flex")],
        securities=[_sec("AAPL"), _sec("UNREFERENCED")],
    )
    policy = OwnershipPolicy(explicit={"flex": {"U1"}})
    accounts, securities, holdings, activities, lots = _filter_owned(snap, policy)
    assert [s.ticker for s in securities] == ["AAPL"]


def test_ingest_skips_connector_that_raises_keeps_last_good():
    good = CanonicalStore()
    good.merge("snaptrade", [_acct("U1")], [], [_hold("U1", "AAPL")], [], [])
    boomer = _FakeConnector("flex", raises=True)
    store = ingest([boomer], OwnershipPolicy(default_source="snaptrade"), store=good, persist=False)
    assert store.holdings["U1"]  # last-good intact


def test_ingest_skips_connector_with_error_snapshot():
    errored = _FakeConnector("flex", ConnectorSnapshot(source="flex", error="token expired"))
    store = ingest([errored], OwnershipPolicy(explicit={"flex": {"U1"}}), store=CanonicalStore(), persist=False)
    assert store.all_accounts() == []


def test_ingest_lands_bronze_when_raw_payload_supplied(tmp_path, monkeypatch):
    landed = {}
    monkeypatch.setattr(
        "portfolio_analytics.ingestion.ingest.save_bronze", lambda src, payload: landed.update({src: payload})
    )
    snap = _FakeConnector("flex", _snap("flex", [_acct("U1", source="flex")]))
    ingest(
        [snap],
        OwnershipPolicy(explicit={"flex": {"U1"}}),
        store=CanonicalStore(),
        persist=False,
        raw_payloads={"flex": "<xml/>"},
    )
    assert landed == {"flex": "<xml/>"}


def test_ingest_persists_when_requested(tmp_path, monkeypatch):
    saved = {}
    monkeypatch.setattr("portfolio_analytics.ingestion.ingest.save_store", lambda store: saved.update({"called": True}))
    snap = _FakeConnector("snaptrade", _snap("snaptrade", [_acct("U1")]))
    ingest([snap], OwnershipPolicy(default_source="snaptrade"), store=CanonicalStore(), persist=True)
    assert saved.get("called")
