"""Showcase Portfolio's frozen sample sleeve (metron-ops#42) — the no-auth `/demo` entry
point's asset-class/tax breadth fixture, folded into REFERENCE_PORTFOLIO_ID. Seeded,
renders end-to-end, and is READ-ONLY (the demo tenant refuses every mutating request)."""

from __future__ import annotations

import io
from datetime import date

import pytest
from sqlalchemy import select

from api.db import models
from api.services import demo

DEMO_HEADERS = {"X-Tenant-Id": str(demo.DEMO_TENANT_ID)}


def test_sample_sleeve_seed_is_idempotent(db_session):
    demo.ensure_reference_seeded(db_session)
    demo.ensure_reference_seeded(db_session)  # re-seeding must not duplicate accounts
    accounts = db_session.scalars(
        select(models.Account).where(
            models.Account.portfolio_id == demo.REFERENCE_PORTFOLIO_ID,
            models.Account.broker == demo._SAMPLE_SLEEVE_SOURCE,
        )
    ).all()
    assert len(accounts) == 2  # Sample Brokerage + Sample IRA, exactly once


def test_sample_sleeve_reconciles_retired_symbols(db_session):
    """metron-ops-I201: an already-deployed instance seeded under an older fixture
    version (that carried AAPL/MSFT, retired by metron-PR230 on 2026-07-13) must
    self-heal on the next ``ensure_reference_seeded`` call — the prior
    ``_ensure_sample_sleeve_seeded`` only checked "has the sleeve ever been seeded"
    and skipped forever once true, so a fixture edit never reached already-persisted
    data. This reproduces that exact scenario against the real reconcile path."""
    from api.services import persistence
    from portfolio_analytics.broker_io.csv_import import parse_transactions_csv

    # Simulate the pre-PR230 seed: the old fixture carried AAPL/MSFT alongside VOO,
    # under the same account labels the current fixture still uses.
    old_csv = (
        "date,type,symbol,quantity,price,amount,account\n"
        "2024-01-08,BUY,AAPL,40,185,7400,Sample Brokerage\n"
        "2024-01-08,BUY,VOO,15,440,6600,Sample Brokerage\n"
        "2024-01-16,BUY,MSFT,20,390,7800,Sample IRA\n"
    )
    result = parse_transactions_csv(old_csv, source=demo._SAMPLE_SLEEVE_SOURCE)
    persistence.persist_snapshot(
        db_session,
        tenant_id=demo.DEMO_TENANT_ID,
        portfolio_id=demo.REFERENCE_PORTFOLIO_ID,
        snapshot=result.snapshot,
    )
    aapl = db_session.scalars(select(models.Security).where(models.Security.symbol == "AAPL")).one()
    db_session.add(
        models.PriceBar(
            security_id=aapl.id, bar_date=demo._SAMPLE_SLEEVE_PRICE_AS_OF, close=210.0, currency="USD"
        )
    )
    db_session.commit()

    # Run the real startup path against the CURRENT (trimmed) fixture.
    demo.ensure_reference_seeded(db_session)

    tickers = set(
        db_session.scalars(
            select(models.Security.symbol)
            .join(models.Transaction, models.Transaction.security_id == models.Security.id)
            .join(models.Account, models.Transaction.account_id == models.Account.id)
            .where(
                models.Account.portfolio_id == demo.REFERENCE_PORTFOLIO_ID,
                models.Account.broker == demo._SAMPLE_SLEEVE_SOURCE,
            )
        ).all()
    )
    assert "AAPL" not in tickers and "MSFT" not in tickers
    assert {"DEMO-VOO", "DEMO-UST-2026", "DEMO-MMF"} <= tickers

    # The retired symbol's GLOBAL price bar is deliberately NOT pruned here any more
    # (metron-ops-I319): "AAPL" is outside the reserved demo namespace, so a bar on
    # this date may equally have come from a real tenant's own refresh, and a date-only
    # delete cannot tell the two apart — which was the same defect this issue fixed one
    # table over. Bars the pre-I319 sleeve actually wrote under a real ticker are
    # removed by ``_repair_legacy_sample_sleeve`` instead, under an exact-close check.
    # (AAPL/MSFT left the fixture in metron-PR230 and their bars were pruned then, by
    # the version of this prune that still did it.)
    remaining_bar = db_session.scalars(
        select(models.PriceBar).where(
            models.PriceBar.security_id == aapl.id,
            models.PriceBar.bar_date == demo._SAMPLE_SLEEVE_PRICE_AS_OF,
        )
    ).first()
    assert remaining_bar is not None and float(remaining_bar.close) == 210.0

    # NAV/holdings totals recompute off the reconciled sleeve, not the stale one.
    value, cost_basis = demo._sample_sleeve_totals(db_session)
    assert (value, cost_basis) == (14300.0, 13500.0)


def test_demo_holdings_span_asset_classes(client, db_session):
    demo.ensure_reference_seeded(db_session)
    r = client.get(f"/portfolios/{demo.REFERENCE_PORTFOLIO_ID}/holdings", headers=DEMO_HEADERS)
    assert r.status_code == 200
    body = r.json()
    # The sample sleeve deliberately spans accounts + NON-EQUITY asset classes (ETF / bond /
    # cash) so the tax-status (#46) and security-type (#47) groupings both showcase on it —
    # it must never carry individual-stock equity of its own (that would inflate the
    # Showcase Portfolio's equity count beyond what Crucible's live sleeve actually holds).
    tickers = {h["ticker"] for h in body}
    assert {"DEMO-VOO", "DEMO-UST-2026", "DEMO-MMF"} <= tickers
    assert "AAPL" not in tickers and "MSFT" not in tickers
    # Holdings value off the seeded frozen prices (no live refresh needed).
    assert all(h["market_value"] is not None for h in body)


def test_demo_has_two_tax_groups(client, db_session):
    demo.ensure_reference_seeded(db_session)
    r = client.get(f"/portfolios/{demo.REFERENCE_PORTFOLIO_ID}/accounts", headers=DEMO_HEADERS)
    assert r.status_code == 200
    treatments = {a["tax_treatment"] for a in r.json()}
    # A taxable (null -> derived) + a tax-deferred account so the #46 grouping shows.
    assert "tax_deferred" in treatments


def test_demo_is_read_only_refresh(client, db_session):
    demo.ensure_reference_seeded(db_session)
    r = client.post(f"/portfolios/{demo.REFERENCE_PORTFOLIO_ID}/prices/refresh", headers=DEMO_HEADERS)
    assert r.status_code == 403
    assert "read-only" in r.json()["detail"].lower()


def test_demo_is_read_only_import(client, db_session):
    demo.ensure_reference_seeded(db_session)
    csv = "date,type,symbol,quantity,price,amount,account\n2024-01-01,BUY,TSLA,1,100,100,Sample Brokerage\n"
    r = client.post(
        f"/portfolios/{demo.REFERENCE_PORTFOLIO_ID}/import/csv",
        files={"file": ("t.csv", io.BytesIO(csv.encode()), "text/csv")},
        headers=DEMO_HEADERS,
    )
    assert r.status_code == 403


def test_demo_is_read_only_patch(client, db_session):
    demo.ensure_reference_seeded(db_session)
    r = client.patch(
        f"/portfolios/{demo.REFERENCE_PORTFOLIO_ID}", json={"name": "Hijacked"}, headers=DEMO_HEADERS
    )
    assert r.status_code == 403


def test_non_demo_tenant_still_writable(client):
    """The read-only guard is demo-only — a normal tenant's POST is unaffected."""
    import uuid

    tenant = str(uuid.uuid4())
    pid = client.post("/portfolios", json={"name": "Real"}, headers={"X-Tenant-Id": tenant}).json()["id"]
    csv = "date,type,symbol,quantity,price,amount,account\n2024-01-01,BUY,AAPL,1,100,100,Brokerage\n"
    r = client.post(
        f"/portfolios/{pid}/import/csv",
        files={"file": ("t.csv", io.BytesIO(csv.encode()), "text/csv")},
        headers={"X-Tenant-Id": tenant},
    )
    assert r.status_code == 200


# ── metron-ops-I319: the sleeve must never touch global reference data ───────
#
# ``securities`` and ``price_bars`` are GLOBAL, cross-tenant tables. Until I319 the
# sample sleeve wrote a synthetic 2024-06-28 close and overwrote name/asset_class on
# the REAL VOO / 912828YK0 / VMFXX rows every real tenant holding those symbols reads.
# These four tests pin the namespace, the guard, the automatic repair and the totals.


_PRE_I319_CSV = (
    "date,type,symbol,quantity,price,amount,account\n"
    "2024-01-08,BUY,VOO,15,440,6600,Sample Brokerage\n"
    "2024-02-02,BUY,912828YK0,50,98,4900,Sample IRA\n"
    "2024-03-15,BUY,VMFXX,2000,1,2000,Sample Brokerage\n"
)


def _seed_pre_i319_sleeve(db_session):
    """Reproduce an already-deployed, pre-metron-ops-I319 instance: the sample sleeve
    persisted against the REAL global VOO / 912828YK0 / VMFXX securities, with this
    module's synthetic closes and metadata overwrites on those global rows. Returns
    ``{symbol: Security}``."""
    from api.services import persistence
    from portfolio_analytics.broker_io.csv_import import parse_transactions_csv

    result = parse_transactions_csv(_PRE_I319_CSV, source=demo._SAMPLE_SLEEVE_SOURCE)
    persistence.persist_snapshot(
        db_session,
        tenant_id=demo.DEMO_TENANT_ID,
        portfolio_id=demo.REFERENCE_PORTFOLIO_ID,
        snapshot=result.snapshot,
    )
    secs = {
        sec.symbol: sec
        for sec in db_session.scalars(
            select(models.Security).where(models.Security.symbol.in_(list(demo._LEGACY_SLEEVE_SYMBOLS)))
        ).all()
    }
    for symbol, (name, asset_class) in demo._LEGACY_SLEEVE_SECURITY_META.items():
        secs[symbol].name, secs[symbol].asset_class = name, asset_class
        db_session.add(
            models.PriceBar(
                security_id=secs[symbol].id,
                bar_date=demo._SAMPLE_SLEEVE_PRICE_AS_OF,
                close=demo._LEGACY_SLEEVE_SYNTHETIC_CLOSES[symbol],
                currency="USD",
            )
        )
    db_session.commit()
    return secs


def _real_voo_with_real_bars(db_session):
    """A real tenant's global VOO row with two genuine, spine-sourced bars — one of
    them ON the sleeve's old marker date, at a close that is NOT the fixture's 490.0."""
    voo = models.Security(symbol="VOO", name="Vanguard S&P 500 ETF", asset_class="etf", currency="USD")
    db_session.add(voo)
    db_session.flush()
    db_session.add_all(
        [
            models.PriceBar(security_id=voo.id, bar_date=demo._SAMPLE_SLEEVE_PRICE_AS_OF, close=487.25, currency="USD"),
            models.PriceBar(security_id=voo.id, bar_date=date(2024, 7, 1), close=492.11, currency="USD"),
        ]
    )
    db_session.commit()
    return voo


def test_real_security_and_bars_are_untouched_by_seeding(db_session):
    """The headline invariant: after a full seed, a real tenant's own VOO Security row
    and its real price history are byte-for-byte what they were. Pre-I319 this failed
    on both halves — the sleeve overwrote name/asset_class and inserted its own bar."""
    voo = _real_voo_with_real_bars(db_session)
    before = (voo.name, voo.asset_class, voo.sector)

    demo.ensure_reference_seeded(db_session)
    demo.ensure_reference_seeded(db_session)  # and again — idempotent

    db_session.refresh(voo)
    assert (voo.name, voo.asset_class, voo.sector) == before
    bars = {
        row.bar_date: float(row.close)
        for row in db_session.scalars(select(models.PriceBar).where(models.PriceBar.security_id == voo.id)).all()
    }
    assert bars == {demo._SAMPLE_SLEEVE_PRICE_AS_OF: 487.25, date(2024, 7, 1): 492.11}
    # And the sleeve itself is fully seeded, under its own namespaced securities.
    assert demo._sample_sleeve_totals(db_session) == (14300.0, 13500.0)


def test_guards_raise_for_a_non_namespaced_symbol(db_session, monkeypatch):
    """Both demo write sites that touch a GLOBAL table hard-refuse a bare real ticker.
    A regression that reintroduced the defect would trip here rather than shipping."""
    demo.ensure_reference_seeded(db_session)

    monkeypatch.setitem(demo._SAMPLE_SLEEVE_PRICES, "VOO", 490.0)
    with pytest.raises(ValueError, match="non-namespaced symbol"):
        demo._seed_sample_sleeve_prices(db_session)
    monkeypatch.delitem(demo._SAMPLE_SLEEVE_PRICES, "VOO")

    monkeypatch.setitem(demo._SAMPLE_SLEEVE_SECURITY_META, "VOO", ("Vanguard S&P 500 ETF", "etf"))
    with pytest.raises(ValueError, match="non-namespaced symbol"):
        demo._apply_sample_sleeve_security_meta(db_session)


def test_repair_deletes_only_the_exact_synthetic_bar(db_session):
    """The repair is keyed on EXACT equality with the value this module seeded. A
    2024-06-28 VOO bar at any other close came from a real refresh and must survive —
    the issue's named gotcha."""
    secs = _seed_pre_i319_sleeve(db_session)
    # A real data-spine refresh has since overwritten OUR VOO bar with a real close.
    voo_bar = db_session.scalars(
        select(models.PriceBar).where(
            models.PriceBar.security_id == secs["VOO"].id,
            models.PriceBar.bar_date == demo._SAMPLE_SLEEVE_PRICE_AS_OF,
        )
    ).one()
    voo_bar.close = 487.25
    db_session.commit()

    actions = demo._repair_legacy_sample_sleeve(db_session)

    # 912828YK0's bar still holds the exact synthetic 99.0 -> deleted.
    assert (
        db_session.scalars(
            select(models.PriceBar).where(
                models.PriceBar.security_id == secs["912828YK0"].id,
                models.PriceBar.bar_date == demo._SAMPLE_SLEEVE_PRICE_AS_OF,
            )
        ).first()
        is None
    )
    # VOO's does not -> kept, untouched.
    kept = db_session.scalars(
        select(models.PriceBar).where(
            models.PriceBar.security_id == secs["VOO"].id,
            models.PriceBar.bar_date == demo._SAMPLE_SLEEVE_PRICE_AS_OF,
        )
    ).one()
    assert float(kept.close) == 487.25
    # Every action is logged with before/after, so the deploy that repaired says what it did.
    assert any(line.startswith("DELETED 912828YK0") for line in actions["price_bars_deleted"])
    assert any(line.startswith("KEPT VOO") for line in actions["price_bars_deleted"])
    # Idempotent, and self-terminating: the sleeve no longer references the legacy
    # securities at all, so a second run is admitted for nothing.
    assert demo._repair_legacy_sample_sleeve(db_session) == {
        "price_bars_deleted": [], "metadata_cleared": [], "ledger_rows_deleted": []
    }


def test_repair_never_touches_a_global_row_the_sleeve_never_wrote(db_session):
    """The admission check. On an instance that never persisted the pre-I319 sleeve, a
    real tenant's VOO row is not read, cleared or deleted — even though its symbol is
    in the legacy table and its name happens to match what the sleeve used to write."""
    voo = models.Security(symbol="VOO", name="Vanguard S&P 500 ETF", asset_class="etf", currency="USD")
    db_session.add(voo)
    db_session.flush()
    db_session.add(
        models.PriceBar(
            security_id=voo.id, bar_date=demo._SAMPLE_SLEEVE_PRICE_AS_OF, close=490.0, currency="USD"
        )
    )
    db_session.commit()
    demo.ensure_reference_seeded(db_session)

    db_session.refresh(voo)
    assert (voo.name, voo.asset_class) == ("Vanguard S&P 500 ETF", "etf")
    assert (
        db_session.scalars(
            select(models.PriceBar).where(
                models.PriceBar.security_id == voo.id,
                models.PriceBar.bar_date == demo._SAMPLE_SLEEVE_PRICE_AS_OF,
            )
        ).one()
        is not None
    )


def test_repair_clears_only_metadata_it_overwrote(db_session):
    """Deliverable 3's metadata half: NULL the fields only where BOTH still equal the
    exact pair this module wrote, so the normal classification path repopulates them —
    and leave a row a real refresh has since renamed strictly alone."""
    secs = _seed_pre_i319_sleeve(db_session)
    secs["VOO"].name = "Vanguard 500 Index Fund ETF"  # a real refresh has since renamed it
    db_session.commit()

    actions = demo._repair_legacy_sample_sleeve(db_session)

    db_session.refresh(secs["VMFXX"])
    db_session.refresh(secs["VOO"])
    assert (secs["VMFXX"].name, secs["VMFXX"].asset_class) == (None, None)
    assert (secs["VOO"].name, secs["VOO"].asset_class) == ("Vanguard 500 Index Fund ETF", "etf")
    assert any(line.startswith("CLEARED VMFXX") for line in actions["metadata_cleared"])


def test_full_startup_repairs_a_pre_i319_instance_end_to_end(db_session):
    """Deliverable 3 as it actually runs: ``ensure_reference_seeded`` — the function
    api/main.py's startup hook calls on EVERY boot — repairs the global rows and
    re-seeds the sleeve under the namespace in one pass, with no operator step."""
    secs = _seed_pre_i319_sleeve(db_session)

    demo.ensure_reference_seeded(db_session)

    for symbol in demo._LEGACY_SLEEVE_SYMBOLS:
        db_session.refresh(secs[symbol])
        assert (secs[symbol].name, secs[symbol].asset_class) == (None, None)
        assert (
            db_session.scalars(
                select(models.PriceBar).where(
                    models.PriceBar.security_id == secs[symbol].id,
                    models.PriceBar.bar_date == demo._SAMPLE_SLEEVE_PRICE_AS_OF,
                )
            ).first()
            is None
        )
    # The sleeve is whole again, under namespaced securities, with the same totals.
    tickers = set(
        db_session.scalars(
            select(models.Security.symbol)
            .join(models.Transaction, models.Transaction.security_id == models.Security.id)
            .join(models.Account, models.Transaction.account_id == models.Account.id)
            .where(
                models.Account.portfolio_id == demo.REFERENCE_PORTFOLIO_ID,
                models.Account.broker == demo._SAMPLE_SLEEVE_SOURCE,
            )
        ).all()
    )
    assert tickers == {"DEMO-VOO", "DEMO-UST-2026", "DEMO-MMF"}
    assert demo._sample_sleeve_totals(db_session) == (14300.0, 13500.0)


def test_showcase_holdings_match_sleeve_totals_after_a_real_voo_refresh(client, db_session):
    """A real tenant's VOO refresh advancing the GLOBAL latest close no longer moves
    the Showcase — the sleeve values off its own namespaced securities, so Holdings
    still equals ``_sample_sleeve_totals`` exactly. Pre-I319 the two diverged."""
    voo = _real_voo_with_real_bars(db_session)
    demo.ensure_reference_seeded(db_session)
    # A real refresh lands a much later, much higher close on the global VOO row.
    db_session.add(models.PriceBar(security_id=voo.id, bar_date=date(2026, 9, 15), close=612.40, currency="USD"))
    db_session.commit()

    r = client.get(f"/portfolios/{demo.REFERENCE_PORTFOLIO_ID}/holdings", headers=DEMO_HEADERS)
    assert r.status_code == 200
    sleeve_value, _cost = demo._sample_sleeve_totals(db_session)
    rendered = sum(
        h["market_value"] for h in r.json()
        if h["ticker"] in demo._SAMPLE_SLEEVE_PRICES and h["market_value"] is not None
    )
    assert rendered == pytest.approx(sleeve_value)
    assert rendered == pytest.approx(14300.0)
