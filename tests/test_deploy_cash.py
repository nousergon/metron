"""Deploy cash (metron-ops#300, Brian ruling 2026-09-14) — the ranking-under-constraints
plan, its constraint block, its champion register, its feed gate, and a golden plan for one
fixture portfolio.

Pure unit tests against an in-memory DB with injected readers — no S3, no network. The
clock is PINNED (``_NOW``) rather than anchored to wall time: a rating artifact's freshness
is judged against ``now``, and a fixture anchored to the wall clock expires at UTC midnight
(that failure has already ejected one docs-only PR from the merge queue).
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

import jsonschema
import pytest

from api.config import settings
from api.db import models
from api.services import deploy_cash, technical_rating

_NOW = datetime(2026, 9, 14, 15, 0, 0, tzinfo=UTC)  # mid-session ET, fixed
_AS_OF = date(2026, 9, 14)
_ARTIFACT_AS_OF = "2026-09-14T14:55:00Z"

GOLDEN_PATH = Path(__file__).parent / "golden" / "deploy_cash_plan.json"
RATINGS_SCHEMA_PATH = Path(__file__).parent / "contracts" / "technical_ratings.schema.json"

# The fixture book: three held names, three watched. Chosen so ONE plan exercises every
# constraint at once — a position cap that blocks a name outright, a position cap that
# merely sizes a line, a sector cap that binds tighter than the position cap, the minimum
# line size, whole-share flooring, and the label filter.
_HELD = {
    # ticker: (shares, price, sector)
    "AAPL": (10, 200.0, "Information Technology"),
    "MSFT": (2, 400.0, "Information Technology"),
    "XOM": (10, 100.0, "Energy"),
}
_WATCHED = {
    "KO": (60.0, "Consumer Staples"),
    "JNJ": (150.0, "Health Care"),
    "NVDA": (50.0, "Information Technology"),
}
_RATINGS = {
    "KO": {"score": 0.8, "label": "Strong Buy", "ma_score": 0.9, "osc_score": 0.7, "n_votes": 11},
    "AAPL": {"score": 0.7, "label": "Buy", "ma_score": 0.8, "osc_score": 0.6, "n_votes": 11},
    "JNJ": {"score": 0.4, "label": "Buy", "ma_score": 0.5, "osc_score": 0.3, "n_votes": 11},
    "NVDA": {"score": 0.4, "label": "Buy", "ma_score": 0.4, "osc_score": 0.4, "n_votes": 11},
    "MSFT": {"score": 0.3, "label": "Buy", "ma_score": 0.3, "osc_score": 0.3, "n_votes": 11},
    "XOM": {"score": 0.0, "label": "Neutral", "ma_score": 0.0, "osc_score": 0.0, "n_votes": 11},
}


def _ratings_artifact(ratings: dict | None = None) -> dict:
    return {
        "schema_version": 1,
        "as_of_utc": _ARTIFACT_AS_OF,
        "quote_as_of_utc": _ARTIFACT_AS_OF,
        "source": "computed_intraday",
        "ratings": {
            sym: {**body, "basis": "intraday"} for sym, body in (ratings if ratings is not None else _RATINGS).items()
        },
    }


def _snapshot(ratings: dict | None = None):
    """A ``RatingSnapshot`` built through the REAL consumer, off an artifact pinned to the
    producer schema — so these tests break if the artifact shape drifts, not just if the
    plan math does."""
    art = _ratings_artifact(ratings)
    jsonschema.validate(art, json.loads(RATINGS_SCHEMA_PATH.read_text()))
    return technical_rating.load_technical_rating(
        intraday_reader=lambda: art, technicals_reader=lambda: {}, now=_NOW
    )


def _seed(session, *, held=None, watched=None, sectors: dict[str, str | None] | None = None, priced=True):
    """The fixture portfolio: held positions (BUY ledger + a cached close) plus watchlist
    rows. ``sectors`` overrides the per-security classification (None = unclassified)."""
    held = _HELD if held is None else held
    watched = _WATCHED if watched is None else watched

    tenant = models.Tenant(name="t")
    session.add(tenant)
    session.flush()
    pf = models.Portfolio(tenant_id=tenant.id, name="P", base_currency="USD")
    session.add(pf)
    session.flush()
    acct = models.Account(
        tenant_id=tenant.id, portfolio_id=pf.id, broker="csv", external_id="CSV-1", currency="USD"
    )
    session.add(acct)
    session.flush()

    def _sector(sym: str, default: str | None) -> str | None:
        return sectors.get(sym, default) if sectors is not None and sym in sectors else default

    for sym, (shares, price, sector) in held.items():
        sec = models.Security(symbol=sym, currency="USD", sector=_sector(sym, sector))
        session.add(sec)
        session.flush()
        session.add(models.Transaction(
            tenant_id=tenant.id, account_id=acct.id, security_id=sec.id, txn_type="BUY",
            quantity=shares, price=price, amount=shares * price, currency="USD",
            trade_date=date(2026, 1, 2), source_key=f"buy-{sym}",
        ))
        if priced:
            session.add(models.PriceBar(security_id=sec.id, bar_date=date(2026, 9, 13), close=price, currency="USD"))

    for sym, (price, sector) in watched.items():
        sec = models.Security(symbol=sym, currency="USD", sector=_sector(sym, sector))
        session.add(sec)
        session.flush()
        if priced:
            session.add(models.PriceBar(security_id=sec.id, bar_date=date(2026, 9, 13), close=price, currency="USD"))
        session.add(models.WatchlistItem(tenant_id=tenant.id, portfolio_id=pf.id, symbol=sym))

    session.commit()
    return tenant.id, pf.id


def _plan(session, tenant_id, pid, amount=10_000.0, *, config=None, ratings=None):
    return deploy_cash.recommend(
        session, tenant_id, pid, amount,
        as_of=_AS_OF, feed_entitled=True, now=_NOW,
        config=config or deploy_cash.DEFAULT_CONFIG,
        ratings=_snapshot(ratings) if not isinstance(ratings, technical_rating.RatingSnapshot) else ratings,
    )


def _by_ticker(plan) -> dict[str, deploy_cash.DeployCashLine]:
    return {line.ticker: line for line in plan.lines}


def _skip_reason(plan, ticker: str) -> str | None:
    for row in plan.skipped:
        if row["ticker"] == ticker:
            return row["reason"]
    return None


# ── The champion register (champion-challenger-policy §4, §9, §10) ────────────
def test_live_champion_is_registered_and_the_serving_path_resolves_it():
    """The pointer names a registered arm, and the serving path goes THROUGH the register.
    §4: 'a slot's live ranking is resolved from the register, never imported directly' — the
    vacuity that rule exists for is a live path importing one function while the register
    still names another, with nothing reading both sides."""
    assert deploy_cash.LIVE_CHAMPION in deploy_cash.RANKERS
    assert deploy_cash.live_ranker() is deploy_cash.RANKERS[deploy_cash.LIVE_CHAMPION]
    assert deploy_cash.SLOT["champion"] == deploy_cash.LIVE_CHAMPION


def test_the_plan_ranks_through_the_register_not_a_direct_import(db_session, monkeypatch):
    """Swap the registered arm's ranking callable and the PLAN's order changes — proof the
    serving path reads the register rather than calling a module-level function."""
    tenant_id, pid = _seed(db_session)
    normal = _plan(db_session, tenant_id, pid)
    assert [line.ticker for line in normal.lines][0] == "KO"

    reversed_spec = dataclasses.replace(
        deploy_cash.RANKERS[deploy_cash.LIVE_CHAMPION],
        rank=lambda candidates, config: sorted(candidates, key=lambda c: (c.score, c.ticker)),
    )
    monkeypatch.setitem(deploy_cash.RANKERS, deploy_cash.LIVE_CHAMPION, reversed_spec)
    flipped = _plan(db_session, tenant_id, pid)
    assert [line.ticker for line in flipped.lines] != [line.ticker for line in normal.lines]


def test_slot_declares_its_arena_parameters_and_carries_no_undeclared_edge_claim():
    """§10 — a slot names its metric, benchmark, count-matching width and every ArenaConfig
    parameter where CI can check them against the code. §4's benchmark rule: a selection-
    stage slot is graded against the population it drew from, never SPY."""
    for key in (
        "alpha", "diff_clip", "cap", "grace_weeks", "promote_min_weeks",
        "promote_evidence", "min_active_arms", "retired_trailing_cycles", "retire_evidence",
    ):
        assert key in deploy_cash.SLOT["arena_config"], key
    assert "SPY" not in deploy_cash.SLOT["benchmark"]
    # §9.1 bootstrap: served because something must serve, not because it won on evidence.
    assert deploy_cash.SLOT["promotion_source"] == "operator_bootstrap"
    assert deploy_cash.SLOT["challengers"] == []


def test_tie_on_score_breaks_to_the_lower_existing_weight():
    """The champion's declared tie-break (metron-ops#300): equal scores go to the name the
    book is least exposed to, then to ticker so the ordering is total."""
    config = deploy_cash.DEFAULT_CONFIG
    heavy = deploy_cash.Candidate(
        ticker="AAA", price=10.0, price_as_of=None, price_source="eod_close", score=0.5, label="Buy",
        rating_basis="eod", rating_as_of=None, sector="Energy", existing_mv=5_000.0,
        existing_weight=0.05, held=True,
    )
    light = deploy_cash.Candidate(
        ticker="ZZZ", price=10.0, price_as_of=None, price_source="eod_close", score=0.5, label="Buy",
        rating_basis="eod", rating_as_of=None, sector="Energy", existing_mv=0.0,
        existing_weight=0.0, held=False,
    )
    assert [c.ticker for c in deploy_cash.live_ranker().rank([heavy, light], config)] == ["ZZZ", "AAA"]


# ── The disclaimer is verbatim and the plan never claims a forecast ───────────
def test_disclaimer_is_the_issue_text_verbatim():
    """metron-ops#300 §4 fixes this copy exactly; metron-ops#295 measured the rating's IC at
    ~0 over 1-20 days, so the wording is what stops the panel reading as a forecast."""
    assert deploy_cash.DISCLAIMER == (
        "Ranked by technical attractiveness under your position and sector limits. "
        "Describes recent price action; not investment advice."
    )


def test_plan_carries_the_disclaimer_and_the_champion(db_session):
    tenant_id, pid = _seed(db_session)
    plan = _plan(db_session, tenant_id, pid)
    assert plan.disclaimer == deploy_cash.DISCLAIMER
    assert plan.champion == deploy_cash.LIVE_CHAMPION


# ── Constraints, one test each ────────────────────────────────────────────────
def test_position_weight_limit_blocks_a_name_already_over_it(db_session):
    """AAPL is 10 shares x $200 = $2,000 of a $13,800 post-deployment book (14.5%), already
    past the 10% cap — so it gets no line at all, however well it ranks (it ranks 2nd)."""
    tenant_id, pid = _seed(db_session)
    plan = _plan(db_session, tenant_id, pid)
    assert "AAPL" not in _by_ticker(plan)
    assert _skip_reason(plan, "AAPL") == "max_position_weight"


def test_position_weight_limit_sizes_a_line_and_is_reported(db_session):
    """KO is unheld, so the 10% cap is what sizes its line: 10% x $13,800 = $1,380, floored
    to 23 shares at $60."""
    tenant_id, pid = _seed(db_session)
    line = _by_ticker(_plan(db_session, tenant_id, pid))["KO"]
    assert line.shares_est == 23
    assert line.usd == pytest.approx(1_380.0)
    assert "max_position_weight" in line.constraints_hit


def test_sector_limit_binds_tighter_than_the_position_limit(db_session):
    """NVDA's own 10% headroom is $1,380, but Information Technology already holds AAPL +
    MSFT = $2,800 against a 30% ($4,140) sector cap, leaving $1,340 — so the SECTOR limit
    sizes the line and both constraints are reported."""
    tenant_id, pid = _seed(db_session)
    line = _by_ticker(_plan(db_session, tenant_id, pid))["NVDA"]
    assert line.usd == pytest.approx(1_300.0)  # 26 shares x $50, floored under the $1,340 cap
    assert line.constraints_hit == ["max_position_weight", "max_sector_weight"]


def test_minimum_line_size_drops_a_line_rather_than_shrinking_it(db_session):
    """MSFT's remaining sector headroom is $40 — one share costs $400, so no line is placed
    and the reason says which limit produced the $40."""
    tenant_id, pid = _seed(db_session)
    plan = _plan(db_session, tenant_id, pid)
    assert "MSFT" not in _by_ticker(plan)
    assert _skip_reason(plan, "MSFT") == "min_line_usd"


def test_whole_shares_only_floors_every_line(db_session):
    tenant_id, pid = _seed(db_session)
    for line in _plan(db_session, tenant_id, pid).lines:
        assert line.shares_est == int(line.shares_est)
        assert line.usd == pytest.approx(round(line.shares_est * line.price, 2))


def test_fractional_shares_when_whole_shares_only_is_off(db_session):
    tenant_id, pid = _seed(db_session)
    config = dataclasses.replace(deploy_cash.DEFAULT_CONFIG, whole_shares_only=False)
    line = _by_ticker(_plan(db_session, tenant_id, pid, config=config))["JNJ"]
    assert line.usd == pytest.approx(1_380.0)  # the full 10% cap, not floored to 9 shares
    assert line.shares_est == pytest.approx(1_380.0 / 150.0)


def test_neutral_and_sell_labels_are_never_candidates(db_session):
    """XOM is rated Neutral. The plan never proposes a name the rating does not rate
    positively, whatever headroom exists."""
    tenant_id, pid = _seed(db_session)
    plan = _plan(db_session, tenant_id, pid)
    assert "XOM" not in _by_ticker(plan)
    assert _skip_reason(plan, "XOM") == "label"


def test_config_block_defaults_are_the_issue_defaults():
    c = deploy_cash.DEFAULT_CONFIG
    assert (c.max_position_weight, c.max_sector_weight, c.min_line_usd) == (0.10, 0.30, 500.0)
    assert c.whole_shares_only is True
    assert c.eligible_labels == ("Buy", "Strong Buy")


def test_config_overrides_are_honoured_end_to_end(db_session):
    """A wider position cap moves the binding constraint, proving the block is read rather
    than shadowed by a literal somewhere in the loop."""
    tenant_id, pid = _seed(db_session)
    config = dataclasses.replace(deploy_cash.DEFAULT_CONFIG, max_position_weight=0.20)
    plan = _plan(db_session, tenant_id, pid, config=config)
    assert _by_ticker(plan)["KO"].usd > 1_380.0
    assert plan.config.max_position_weight == 0.20


# ── Candidate admission: rated, priced, classified, in the deployment currency ─
def test_unrated_candidate_is_skipped_never_guessed(db_session):
    tenant_id, pid = _seed(db_session)
    ratings = {k: v for k, v in _RATINGS.items() if k != "KO"}
    plan = _plan(db_session, tenant_id, pid, ratings=ratings)
    assert "KO" not in _by_ticker(plan)
    assert _skip_reason(plan, "KO") == "no_rating"


def test_unpriced_candidate_is_skipped(db_session):
    tenant_id, pid = _seed(db_session, watched={"KO": (60.0, "Consumer Staples")}, priced=False)
    plan = _plan(db_session, tenant_id, pid)
    assert plan.lines == []
    assert _skip_reason(plan, "KO") == "unpriced"


def test_unclassified_sector_is_skipped_rather_than_exempted_from_the_cap(db_session):
    """A name with no sector cannot have the sector limit evaluated. Quietly exempting it
    would be the cap silently not applying — so it is refused and said so."""
    tenant_id, pid = _seed(db_session, sectors={"KO": None})
    plan = _plan(db_session, tenant_id, pid)
    assert "KO" not in _by_ticker(plan)
    assert _skip_reason(plan, "KO") == "sector_unknown"


def test_foreign_currency_candidate_is_skipped_not_priced_one_to_one(db_session):
    tenant_id, pid = _seed(db_session)
    sec = db_session.query(models.Security).filter_by(symbol="KO").one()
    sec.currency = "EUR"
    db_session.commit()
    plan = _plan(db_session, tenant_id, pid)
    assert _skip_reason(plan, "KO") == "currency"


def test_watchlist_symbol_currency_resolves_through_the_tenants_own_rows(db_session):
    """metron-ops#351: a watched-but-not-held symbol used to take its currency from the
    global "first Security row per symbol" pick, so a same-symbol decoy under another
    currency (the 2026-08-17 ``1299`` USD-vs-HKD shape) could price it — off the decoy's
    bar — as a base-currency candidate. It must resolve through the Security row this
    tenant's own ledger links to: here a closed-out 1299/HKD position."""
    tenant_id, pid = _seed(db_session, held={}, watched={})
    acct = db_session.query(models.Account).filter_by(tenant_id=tenant_id).one()
    real = models.Security(symbol="1299", yf_symbol="1299.HK", currency="HKD", sector="Financials")
    # Sorts FIRST by id (the old pick) and carries a USD bar, so the old path priced it.
    decoy = models.Security(id=uuid.UUID(int=0), symbol="1299", currency="USD", sector="Financials")
    db_session.add_all([real, decoy])
    db_session.flush()
    for kind, day in (("BUY", date(2026, 1, 2)), ("SELL", date(2026, 3, 2))):
        db_session.add(
            models.Transaction(
                tenant_id=tenant_id,
                account_id=acct.id,
                security_id=real.id,
                txn_type=kind,
                quantity=100,
                price=60.0,
                amount=6000.0,
                currency="HKD",
                trade_date=day,
                source_key=f"{kind}-1299",
            )
        )
    db_session.add_all(
        [
            models.PriceBar(security_id=real.id, bar_date=date(2026, 9, 13), close=75.0, currency="HKD"),
            models.PriceBar(security_id=decoy.id, bar_date=date(2026, 9, 13), close=79.0, currency="USD"),
            models.WatchlistItem(tenant_id=tenant_id, portfolio_id=pid, symbol="1299"),
        ]
    )
    db_session.commit()
    ratings = {"1299": {"score": 0.8, "label": "Strong Buy", "ma_score": 0.9, "osc_score": 0.7, "n_votes": 11}}
    plan = _plan(db_session, tenant_id, pid, ratings=ratings)
    assert "1299" not in _by_ticker(plan)
    assert _skip_reason(plan, "1299") == "currency"


def test_empty_universe_yields_an_empty_plan_not_an_error(db_session):
    tenant_id, pid = _seed(db_session, held={}, watched={})
    plan = _plan(db_session, tenant_id, pid)
    assert plan.lines == []
    assert plan.allocated_usd == 0.0
    assert plan.unallocated_usd == pytest.approx(10_000.0)


# ── Unallocated cash is reported, never forced ────────────────────────────────
def test_unallocated_cash_is_reported_with_reasons(db_session):
    tenant_id, pid = _seed(db_session)
    plan = _plan(db_session, tenant_id, pid)
    assert plan.allocated_usd == pytest.approx(4_030.0)
    assert plan.unallocated_usd == pytest.approx(5_970.0)
    assert plan.allocated_usd + plan.unallocated_usd == pytest.approx(plan.amount_usd)
    assert plan.unallocated_reasons  # never silently empty while cash is left over
    assert any("position-weight limit" in r for r in plan.unallocated_reasons)


def test_a_fully_allocatable_amount_leaves_nothing_unexplained(db_session):
    """With the caps opened up, a small amount fits entirely inside the first line — it is
    fully deployed and no reason is manufactured for a remainder that doesn't exist."""
    tenant_id, pid = _seed(db_session)
    config = dataclasses.replace(deploy_cash.DEFAULT_CONFIG, max_position_weight=1.0, max_sector_weight=1.0)
    plan = _plan(db_session, tenant_id, pid, amount=600.0, config=config)
    assert _by_ticker(plan)["KO"].usd == pytest.approx(600.0)  # 10 shares x $60
    assert plan.unallocated_usd == pytest.approx(0.0)
    assert plan.unallocated_reasons == []


def test_amount_below_the_minimum_line_allocates_nothing_and_says_why(db_session):
    tenant_id, pid = _seed(db_session)
    plan = _plan(db_session, tenant_id, pid, amount=100.0)
    assert plan.lines == []
    assert plan.unallocated_usd == pytest.approx(100.0)
    assert plan.unallocated_reasons


def test_weights_are_measured_against_a_fixed_post_deployment_basis(db_session):
    tenant_id, pid = _seed(db_session)
    plan = _plan(db_session, tenant_id, pid)
    assert plan.portfolio_value == pytest.approx(3_800.0)
    assert plan.deployment_basis == pytest.approx(13_800.0)


def test_every_line_carries_its_reasons(db_session):
    """metron-ops#300 §1: label, score, sub-scores, weight before/after, sector headroom."""
    tenant_id, pid = _seed(db_session)
    for line in _plan(db_session, tenant_id, pid).lines:
        joined = " | ".join(line.reasons)
        assert line.technical_label in joined
        assert "oscillator" in joined
        assert "limit 10%" in joined
        assert "limit 30%" in joined


def test_rating_as_of_is_carried_through_from_the_artifact(db_session):
    tenant_id, pid = _seed(db_session)
    plan = _plan(db_session, tenant_id, pid)
    assert plan.rating_as_of == _ARTIFACT_AS_OF
    assert plan.rating_basis == "intraday"


# ── Golden plan ───────────────────────────────────────────────────────────────
def _serialize(plan) -> dict:
    return json.loads(json.dumps(dataclasses.asdict(plan), default=str, sort_keys=True))


def test_golden_plan_for_the_fixture_portfolio(db_session):
    """The whole plan, byte-for-byte, for one fixture portfolio — the regression net a
    per-constraint test can't provide: it catches an ordering change, a rounding change, or
    a reason string quietly rewritten, not just a cap arithmetic error.

    Regenerate deliberately (never reflexively) with::

        METRON_WRITE_GOLDEN=1 .venv/bin/python -m pytest tests/test_deploy_cash.py -k golden
    """
    import os

    tenant_id, pid = _seed(db_session)
    actual = _serialize(_plan(db_session, tenant_id, pid))
    if os.environ.get("METRON_WRITE_GOLDEN"):  # pragma: no cover - maintenance path
        GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN_PATH.write_text(json.dumps(actual, indent=2, sort_keys=True) + "\n")
    assert actual == json.loads(GOLDEN_PATH.read_text())


# ── The feed gate: owner build serves it, the no-feed beta 404s ───────────────
def _hdr(tenant_id):
    return {"X-Tenant-Id": str(tenant_id)}


def test_owner_build_serves_a_plan(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "tier_simulator", False)
    monkeypatch.setattr(settings, "feed_entitled", True)
    snapshot = _snapshot()
    monkeypatch.setattr(deploy_cash.technical_rating_service, "load_technical_rating", lambda: snapshot)
    tenant_id, pid = _seed(db_session)
    r = client.get(f"/portfolios/{pid}/deploy-cash?amount=10000", headers=_hdr(tenant_id))
    assert r.status_code == 200
    body = r.json()
    assert [line["ticker"] for line in body["lines"]] == ["KO", "JNJ", "NVDA"]
    assert body["disclaimer"] == deploy_cash.DISCLAIMER
    assert body["config"]["max_position_weight"] == 0.10
    assert body["champion"] == deploy_cash.LIVE_CHAMPION


def test_beta_no_feed_build_gets_404_and_never_reads_the_rating(client, db_session, monkeypatch):
    """The beta must not learn the panel exists — 404, and the reader is never called, so no
    yfinance-derived datum can reach a beta surface (metron-ops#52)."""
    monkeypatch.setattr(settings, "tier_simulator", False)
    monkeypatch.setattr(settings, "feed_entitled", False)

    def _boom():
        raise AssertionError("the rating reader must not be called off a feed-entitled build")

    monkeypatch.setattr(deploy_cash.technical_rating_service, "load_technical_rating", _boom)
    tenant_id, pid = _seed(db_session)
    r = client.get(f"/portfolios/{pid}/deploy-cash?amount=10000", headers=_hdr(tenant_id))
    assert r.status_code == 404


def test_a_non_positive_amount_is_refused(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "feed_entitled", True)
    tenant_id, pid = _seed(db_session)
    assert client.get(f"/portfolios/{pid}/deploy-cash?amount=0", headers=_hdr(tenant_id)).status_code == 422
