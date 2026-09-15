"""External user demo (metron-ops-I310): release gate, invites, pinned sessions, locked
cards, copy lint, funnel counters, and the leak test over every API route.

Uses ``raw_client`` so requests run the REAL ``identity.require_tenant_id`` (which is
where an ``X-Demo-Session`` token is verified) and the real middleware stack.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.routing import APIRoute

from api.config import settings
from api.db import models
from api.main import app
from api.routers import external_demo as router_mod
from api.services import demo_household, technical_rating
from api.services import external_demo as svc
from api.services.auth_jwt import IdentityClaims

HOUSEHOLD = str(demo_household.DEMO_HOUSEHOLD_PORTFOLIO_ID)
OWNER_EMAIL = "owner@example.test"


@pytest.fixture()
def released(monkeypatch):
    monkeypatch.setattr(settings, "external_demo_released", True)


@pytest.fixture()
def seeded(db_session):
    demo_household.ensure_demo_household_seeded(db_session)
    return db_session


@pytest.fixture()
def demo_headers(released, seeded):
    code, _ = svc.create_invite(seeded)
    token, _ = svc.redeem_invite(seeded, code)
    return {"X-Demo-Session": token}


@pytest.fixture()
def owner(monkeypatch):
    """A verified identity listed as an external-demo owner."""
    monkeypatch.setattr(settings, "external_demo_admin_emails", OWNER_EMAIL)
    monkeypatch.setattr(router_mod, "verify_identity_token", lambda _t: IdentityClaims(sub="s", email=OWNER_EMAIL))
    return {"Authorization": "Bearer stand-in"}


# ── Release gate ─────────────────────────────────────────────────────────────
def test_release_flag_defaults_off():
    assert type(settings).model_fields["external_demo_released"].default is False


def test_invite_creation_is_403_while_unreleased(raw_client, owner):
    assert settings.external_demo_released is False
    r = raw_client.post("/external-demo/invites", headers=owner)
    assert r.status_code == 403


def test_invite_creation_is_403_while_unreleased_without_auth(raw_client):
    assert raw_client.post("/external-demo/invites").status_code == 403


def test_redemption_is_403_while_unreleased(raw_client, seeded, monkeypatch):
    monkeypatch.setattr(settings, "external_demo_released", True)
    code, _ = svc.create_invite(seeded)
    monkeypatch.setattr(settings, "external_demo_released", False)
    assert raw_client.post("/external-demo/sessions", json={"code": code}).status_code == 403


def test_existing_session_dies_when_flag_is_off(raw_client, demo_headers, monkeypatch):
    assert raw_client.get("/me", headers=demo_headers).status_code == 200
    monkeypatch.setattr(settings, "external_demo_released", False)
    assert raw_client.get("/me", headers=demo_headers).status_code == 401


# ── Invites ──────────────────────────────────────────────────────────────────
def test_owner_creates_invite_and_viewer_redeems_once(raw_client, released, seeded, owner):
    r = raw_client.post("/external-demo/invites", headers=owner)
    assert r.status_code == 201, r.text
    code = r.json()["code"]
    first = raw_client.post("/external-demo/sessions", json={"code": code})
    assert first.status_code == 201
    assert first.json()["portfolio_id"] == HOUSEHOLD
    assert raw_client.post("/external-demo/sessions", json={"code": code}).status_code == 403
    # Only digests are stored.
    assert seeded.query(models.ExternalDemoInvite).filter_by(code_digest=code).count() == 0


def test_non_owner_cannot_create_invites(raw_client, released, monkeypatch):
    monkeypatch.setattr(settings, "external_demo_admin_emails", OWNER_EMAIL)
    monkeypatch.setattr(router_mod, "verify_identity_token", lambda _t: IdentityClaims(sub="s", email="x@example.test"))
    assert raw_client.post("/external-demo/invites", headers={"Authorization": "Bearer t"}).status_code == 403


def test_empty_owner_list_fails_closed(raw_client, released, monkeypatch):
    monkeypatch.setattr(router_mod, "verify_identity_token", lambda _t: IdentityClaims(sub="s", email=OWNER_EMAIL))
    assert raw_client.post("/external-demo/invites", headers={"Authorization": "Bearer t"}).status_code == 403


def test_expired_invite_is_refused(raw_client, released, seeded):
    code, invite = svc.create_invite(seeded)
    invite.expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
    seeded.commit()
    assert raw_client.post("/external-demo/sessions", json={"code": code}).status_code == 403


def test_unknown_session_token_is_401(raw_client, released, seeded):
    assert raw_client.get("/me", headers={"X-Demo-Session": "nope"}).status_code == 401


# ── Pinned session ───────────────────────────────────────────────────────────
def _features(body: dict) -> dict[str, bool]:
    return {f["key"]: f["available"] for f in body["features"]}


def test_session_resolves_to_pinned_no_advice_set_with_feed_on(raw_client, demo_headers, monkeypatch):
    # The deployment is the beta without a feed and the simulator is on — the pin wins.
    monkeypatch.setattr(settings, "default_tier", "beta")
    monkeypatch.setattr(settings, "feed_entitled", False)
    monkeypatch.setattr(settings, "tier_simulator", True)
    r = raw_client.get(
        "/meta/entitlements?preview_tier=personal&preview_feed=true", headers=demo_headers
    )
    body = r.json()
    assert body["tier"] == "external_demo" and body["feed_enabled"] is True
    assert body["external_demo"] is True and body["simulator"] is False
    feats = _features(body)
    for key in ("glance", "cash_to_targets", "whatif_purchase", "goal", "risk", "attribution",
                "benchmark", "scenarios", "calendar", "indices"):
        assert feats[key] is True, key
    for key in ("market_board", "research_intel", "alpha_engine", "ai_advisor"):
        assert feats[key] is False, key


def test_fallback_relocks_feed_features(raw_client, demo_headers, monkeypatch):
    monkeypatch.setattr(settings, "external_demo_feed_features_locked", True)
    feats = _features(raw_client.get("/meta/entitlements", headers=demo_headers).json())
    assert not any(feats[k] for k in ("risk", "attribution", "benchmark", "scenarios", "calendar", "indices"))
    assert feats["glance"] is True
    cards = {c["key"] for c in raw_client.get("/external-demo/locked-cards", headers=demo_headers).json()}
    assert {"risk", "attribution", "deploy_cash"} <= cards


def test_owner_view_is_unchanged(raw_client, seeded, monkeypatch):
    monkeypatch.setattr(settings, "default_tier", "personal")
    body = raw_client.get("/meta/entitlements").json()
    assert body["tier"] == "personal" and body["external_demo"] is False
    assert _features(body)["research_intel"] is True


def test_portfolio_list_is_the_household_only(raw_client, demo_headers):
    ids = [p["id"] for p in raw_client.get("/portfolios", headers=demo_headers).json()]
    assert ids == [HOUSEHOLD]


def test_plugins_are_hidden(raw_client, demo_headers):
    assert raw_client.get("/meta/plugins", headers=demo_headers).json() == []


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", f"/portfolios/{HOUSEHOLD}/market-board?scope=held"),
        ("GET", f"/portfolios/{HOUSEHOLD}/deploy-cash"),
        ("GET", "/research-intel"),
        ("GET", "/ext/advisor/anything"),
        ("GET", "/ext/alpha-engine/anything"),
        ("GET", "/portfolios/00000000-0000-0000-0000-00000000de62"),  # the Showcase
        ("GET", "/meta/status"),
        ("GET", "/external-demo/counters"),
        ("POST", "/external-demo/invites"),
        ("PUT", f"/portfolios/{HOUSEHOLD}/goal"),
        ("PUT", f"/portfolios/{HOUSEHOLD}/plan/targets"),
    ],
)
def test_default_deny_routes(raw_client, demo_headers, method, path):
    assert raw_client.request(method, path, headers=demo_headers).status_code == 403


def test_route_policy_is_default_deny_for_unknown_paths():
    assert svc.is_route_allowed("GET", f"/portfolios/{HOUSEHOLD}/holdings")
    assert not svc.is_route_allowed("GET", "/a-route-added-later")
    assert not svc.is_route_allowed("DELETE", f"/portfolios/{HOUSEHOLD}/watchlist/AAPL")


# ── Side-effect-free compute allowlist (metron-ops-I322) ─────────────────────
@pytest.mark.parametrize(
    "path",
    [
        f"/portfolios/{HOUSEHOLD}/plan/cash-to-targets",
        f"/portfolios/{HOUSEHOLD}/plan/whatif",
        f"/portfolios/{HOUSEHOLD}/risk/compute",
        f"/portfolios/{HOUSEHOLD}/attribution/compute",
    ],
)
def test_compute_routes_are_route_allowed(path):
    assert svc.is_route_allowed("POST", path)


def test_compute_allowlist_does_not_widen_by_method_alone():
    """The exemption is per (method, path), never "any POST under the household path" —
    a persisting POST (e.g. a hypothetical future household mutation) stays denied."""
    assert not svc.is_route_allowed("POST", f"/portfolios/{HOUSEHOLD}/plan/targets")
    assert not svc.is_route_allowed("PUT", f"/portfolios/{HOUSEHOLD}/plan/cash-to-targets")
    assert not svc.is_route_allowed("POST", "/portfolios/00000000-0000-0000-0000-00000000de62/plan/cash-to-targets")


# ── Locked cards: copy lint ──────────────────────────────────────────────────
_FORBIDDEN = re.compile(r"advice|recommend|what to buy|adviser|advisor", re.IGNORECASE)


@pytest.mark.parametrize("card", svc.ADVICE_CARDS + svc.FEED_CARDS, ids=lambda c: c.key)
def test_locked_card_copy_lint(card):
    for text in (card.name, card.line, card.status):
        assert text.strip()
        assert not _FORBIDDEN.search(text), f"{card.key}: {text!r}"


def test_advice_cards_carry_the_neutral_status():
    keys = {c.key for c in svc.ADVICE_CARDS}
    assert keys == {"deploy_cash", "market_board", "research_intel", "alpha_engine", "intelligence"}
    assert {c.status for c in svc.ADVICE_CARDS} == {"In development · available after regulatory review"}


def test_copy_lint_catches_a_violation():
    assert _FORBIDDEN.search("Personalised Advice for you")


# ── Funnel counters ──────────────────────────────────────────────────────────
def test_taps_and_counters(raw_client, demo_headers, owner):
    assert raw_client.post("/external-demo/taps", json={"card": "deploy_cash"}, headers=demo_headers).status_code == 204
    assert raw_client.post("/external-demo/taps", json={"card": "deploy_cash"}, headers=demo_headers).status_code == 204
    assert raw_client.post("/external-demo/taps", json={"card": "risk"}, headers=demo_headers).status_code == 422
    assert raw_client.post("/external-demo/taps", json={"card": "deploy_cash"}).status_code == 401
    counts = raw_client.get("/external-demo/counters", headers=owner).json()
    assert counts["invites_created"] == 1
    assert counts["sessions_started"] == 1
    assert counts["locked_card_taps"]["deploy_cash"] == 2
    assert counts["locked_card_taps"]["market_board"] == 0


def test_counters_are_owner_only(raw_client, released, monkeypatch):
    assert raw_client.get("/external-demo/counters").status_code == 401
    monkeypatch.setattr(router_mod, "verify_identity_token", lambda _t: IdentityClaims(sub="s", email="x@example.test"))
    assert raw_client.get("/external-demo/counters", headers={"Authorization": "Bearer t"}).status_code == 403


# ── Leak test: every API route under an external-demo session ────────────────
_PARAM_VALUES = {
    "portfolio_id": HOUSEHOLD,
    "account_id": str(uuid.uuid4()),
    "address_id": str(uuid.uuid4()),
    "authorization_id": "x",
    "symbol": "AAPL",
    "ticker": "AAPL",
}
_DEPLOY_CASH_KEYS = {"champion", "deployment_basis"}
_PAYLOAD_KEYS = {"research_intel", "intel", "alpha_engine", "advisor", "narrative"}


def _routes() -> list[tuple[str, str]]:
    out = []
    for r in app.routes:
        if isinstance(r, APIRoute):
            for m in sorted(r.methods):
                out.append((m, r.path))
    return sorted(set(out))


def _fill(path: str) -> str:
    return re.sub(r"\{(\w+)(?::\w+)?\}", lambda m: _PARAM_VALUES.get(m.group(1), "x"), path)


def _leaks(node, trail="$") -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        keys = set(node)
        if _DEPLOY_CASH_KEYS <= keys:
            found.append(f"{trail}: deploy-cash payload")
        for k, v in node.items():
            if (k.startswith("tech_rating") or k.startswith("attractiveness")) and v not in (None, [], {}):
                found.append(f"{trail}.{k}")
            if k in _PAYLOAD_KEYS and v not in (None, [], {}):
                found.append(f"{trail}.{k}")
            found += _leaks(v, f"{trail}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            found += _leaks(v, f"{trail}[{i}]")
    return found


@pytest.fixture()
def rating_everywhere(monkeypatch, seeded):
    """Make the advice data exist, so a leak would be visible: a fresh rating for every
    security in the test database (the household's ``DEMO-`` symbols included)."""
    symbols = set()
    for sec in seeded.query(models.Security).all():
        symbols.add(sec.symbol)
        if sec.yf_symbol:
            symbols.add(sec.yf_symbol)

    def _art():
        return {
            "schema_version": 1,
            "as_of_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "ratings": {s: {"score": 0.6, "label": "Buy", "ma_score": 0.7, "osc_score": 0.5,
                            "n_buy": 8, "n_neutral": 2, "n_sell": 1, "n_votes": 11} for s in symbols},
        }
    monkeypatch.setattr(technical_rating, "_default_intraday_reader", _art)


def test_leak_harness_detects_a_rating_on_the_owner_path(raw_client, seeded, rating_everywhere, monkeypatch):
    """Control: without the pin the same fixture DOES surface ratings, so the leak test
    below is not vacuously green."""
    monkeypatch.setattr(settings, "feed_entitled", True)
    body = raw_client.get(
        f"/portfolios/{HOUSEHOLD}/holdings",
        headers={"X-Tenant-Id": str(demo_household.DEMO_TENANT_ID)},
    ).json()
    assert _leaks(body), "control failed: the fixture produced no rating to detect"


@pytest.mark.parametrize("method,path", _routes())
def test_no_advice_field_on_any_route(raw_client, demo_headers, rating_everywhere, monkeypatch, method, path):
    monkeypatch.setattr(settings, "feed_entitled", True)
    monkeypatch.setattr(settings, "default_tier", "personal")
    url = _fill(path)
    if "market-board" in path:
        url += "?scope=held"
    r = raw_client.request(method, url, headers=demo_headers, json={} if method in {"POST", "PUT", "PATCH"} else None)
    assert r.status_code < 500, f"{method} {url} -> {r.status_code}"
    if "application/json" in r.headers.get("content-type", ""):
        leaks = _leaks(r.json())
        assert not leaks, f"{method} {url} leaked: {leaks}"
    if path.endswith(("/market-board", "/deploy-cash")) or path.startswith(("/research-intel", "/ext/")):
        assert r.status_code in (403, 404)
