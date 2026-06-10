"""Extension-point contract: discovery, the enabled() gate, fail-loud validation,
and the public-tier default (no plugins → empty nav)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi import APIRouter

from api import plugins
from api.plugins import MetronPlugin, PluginNav, active_plugins


@dataclass
class _FakePlugin:
    """A protocol-satisfying stand-in for a metron-ops plugin."""

    nav: PluginNav
    router: APIRouter
    _on: bool

    def enabled(self) -> bool:
        return self._on


def _make(id: str, *, on: bool) -> _FakePlugin:
    return _FakePlugin(nav=PluginNav(id=id, label=id.title(), href=id, tier="personal"), router=APIRouter(), _on=on)


class _FakeEntryPoint:
    def __init__(self, name, factory):
        self.name = name
        self._factory = factory

    def load(self):
        return self._factory


def _patch_entry_points(monkeypatch, eps):
    # Patch the group lookup so discovery sees exactly `eps` and nothing the host
    # environment happens to have installed.
    def fake_entry_points(*, group):
        assert group == plugins.ENTRY_POINT_GROUP
        return eps

    monkeypatch.setattr("api.plugins.importlib.metadata.entry_points", fake_entry_points)


def test_fake_plugin_satisfies_protocol():
    # runtime_checkable guard the discovery loop relies on.
    assert isinstance(_make("advisor", on=True), MetronPlugin)


def test_no_plugins_is_the_public_default(monkeypatch):
    _patch_entry_points(monkeypatch, [])
    assert active_plugins() == []


def test_enabled_gate_filters_discovered_plugins(monkeypatch):
    on = _make("advisor", on=True)
    off = _make("alpha", on=False)
    _patch_entry_points(
        monkeypatch,
        [_FakeEntryPoint("advisor", lambda: on), _FakeEntryPoint("alpha", lambda: off)],
    )
    active = active_plugins()
    assert [p.nav.id for p in active] == ["advisor"]


def test_non_conforming_factory_fails_loud(monkeypatch):
    _patch_entry_points(monkeypatch, [_FakeEntryPoint("broken", lambda: object())])
    with pytest.raises(TypeError, match="does not satisfy the MetronPlugin protocol"):
        active_plugins()


def test_meta_plugins_endpoint_empty_by_default(client, monkeypatch):
    _patch_entry_points(monkeypatch, [])
    resp = client.get("/meta/plugins")
    assert resp.status_code == 200
    assert resp.json() == []


def test_meta_plugins_endpoint_advertises_active_nav(client, monkeypatch):
    advisor = _make("advisor", on=True)
    _patch_entry_points(monkeypatch, [_FakeEntryPoint("advisor", lambda: advisor)])
    resp = client.get("/meta/plugins")
    assert resp.status_code == 200
    assert resp.json() == [{"id": "advisor", "label": "Advisor", "href": "advisor", "tier": "personal"}]
