"""Extension point — optional, out-of-tree plugins that add premium surfaces.

The public Metron repo ships ONLY this plumbing. Proprietary overlays (the AI
Advisor, the Alpha Engine signal integration) live in the private ``metron-ops``
companion package and register through the ``metron.plugins`` entry-point group.
When that package is not installed — the default for any public / self-host
deploy — discovery returns nothing and Metron stays a pure, descriptive,
no-AI / no-advice product. The open-core boundary is structural: no prompt, no
signal logic, and no premium flag default ever lives in this file.

A plugin contributes (a) a FastAPI router (mounted under ``/ext/...``) and
(b) nav metadata the web surfaces conditionally via ``GET /meta/plugins``. Each
plugin self-gates through ``enabled()`` — a feature flag in its own runtime —
mirroring the zero-footprint ``ai_advisor.enabled`` pattern: installed-but-off
is indistinguishable from absent (no router mounted, no nav advertised, and the
proprietary module's heavy / secret imports are never reached at request time).

An entry point under ``metron.plugins`` must resolve to a **zero-argument
factory** returning a ``MetronPlugin``. Example, in metron-ops' pyproject::

    [project.entry-points."metron.plugins"]
    advisor = "metron_ext.advisor:plugin"
"""

from __future__ import annotations

import importlib.metadata
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from fastapi import APIRouter

ENTRY_POINT_GROUP = "metron.plugins"


@dataclass(frozen=True)
class PluginNav:
    """How a plugin appears in the web nav (rendered only when the plugin is active).

    ``href`` is relative to a portfolio (e.g. ``"advisor"`` → ``/portfolios/{id}/advisor``).
    ``tier`` is informational (``"personal"`` | ``"premium"``) — premium overlays are
    NEVER offered on the public free tier; that boundary is enforced by the plugin not
    being installed there, not by this string.
    """

    id: str
    label: str
    href: str
    tier: str


@runtime_checkable
class MetronPlugin(Protocol):
    """A premium surface contributed by an out-of-tree package.

    The public repo depends only on this Protocol; concrete implementations live in
    metron-ops. ``router`` mounts the plugin's endpoints, ``nav`` describes its web
    entry, and ``enabled()`` is the runtime kill-switch (reads a flag / env var).
    """

    nav: PluginNav
    router: APIRouter

    def enabled(self) -> bool: ...


@runtime_checkable
class ProvidesInvestorTargets(Protocol):
    """Optional plugin capability: the user's own stated investment targets.

    The FREE-tier diagnostics card (metron-ops-I167) evaluates the user's authored
    targets MECHANICALLY ("you set a 10% max position; AAPL is 14%"). The targets live
    in the private advisor plugin's ``AdvisorProfile`` store, so the public repo reads
    them through this capability protocol — never by importing the private package or
    its tables. A plugin exposes it by adding one method; a deploy without such a
    plugin (or with the user having authored nothing) simply has no targets, and the
    drift section doesn't render.

    ``investor_targets`` returns the tenant's ``investor_profile`` config block (the
    schema already mirrored publicly in ``web/lib/api.ts``) or ``None`` when the user
    has stored no profile. Consumers extract ONLY the mechanical target fields
    (``portfolio_analytics.domain.diagnostics.StatedTargets.from_profile_block``);
    suitability fields never reach any generated output (metron-ops-I166).
    """

    def investor_targets(self, session, tenant_id) -> dict | None: ...


def investor_targets(session, tenant_id) -> dict | None:
    """The tenant's stated-targets profile block from the first active plugin that
    provides one, or ``None`` (no capable plugin installed/enabled, or nothing stored).
    Session/tenant types are the API layer's (SQLAlchemy Session, UUID) — untyped here
    so this plumbing module keeps zero DB imports."""
    for plugin in active_plugins():
        if isinstance(plugin, ProvidesInvestorTargets):
            block = plugin.investor_targets(session, tenant_id)
            if block:
                return block
    return None


def _discover() -> list[MetronPlugin]:
    """Load every plugin registered under the ``metron.plugins`` entry-point group.

    Each entry point must resolve to a zero-arg factory returning a ``MetronPlugin``.
    Fail-loud by design: a factory that raises, or returns an object that doesn't
    satisfy the protocol, propagates rather than being silently dropped — a premium
    surface vanishing with no signal is the exact failure mode the registry prevents.
    """
    plugins: list[MetronPlugin] = []
    for ep in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP):
        plugin = ep.load()()
        if not isinstance(plugin, MetronPlugin):
            raise TypeError(
                f"metron plugin '{ep.name}' factory returned {type(plugin)!r}, "
                "which does not satisfy the MetronPlugin protocol (needs nav, router, enabled())."
            )
        plugins.append(plugin)
    return plugins


def active_plugins() -> list[MetronPlugin]:
    """Discovered plugins whose ``enabled()`` gate is on — the set to mount + advertise.

    Called once at app import to mount routers, and per-request by ``GET /meta/plugins``
    so the web only renders nav for surfaces that are actually live.
    """
    return [p for p in _discover() if p.enabled()]
