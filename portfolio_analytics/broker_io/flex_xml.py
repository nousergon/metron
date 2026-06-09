"""IBKR realized gains/losses from an Interactive Brokers **Flex Query**.

SnapTrade exposes **no** activity history for IBKR accounts (verified empty back to
2015), so realized capital gains for an IBKR account can't be reconstructed from
the SnapTrade feed. IBKR's own **Flex Query** is the authoritative source — it
emits, per closed lot, the open/close dates, proceeds, cost basis, and IBKR's
FIFO-computed realized P&L (commission-inclusive). This module fetches + parses
those closed lots into the shared ``analytics.ledger.RealizedGain`` type so the
Tax page's existing realized-by-year render can show them.

Two ingest paths feed one persistent union cache:
  1. **Flex Web Service** (auto-fetch): a token + query id (env) pull the rolling
     window (IBKR caps a single Activity request at 365 days) with no manual step.
  2. **File ingest**: manually-exported Flex XML statements dropped under
     ``cache/ibkr_flex/`` backfill calendar years older than that window.

Both are merged into ``cache/ibkr_flex_lots.json`` (gitignored, survives deploys),
deduped by a stable lot key, so history accumulates and a transient fetch failure
never drops previously-seen lots.

Pure stdlib (``urllib`` + ``xml.etree``) — no Streamlit, no new pip deps — so it's
unit-testable in isolation.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

from portfolio_analytics.domain.ledger import RealizedGain

logger = logging.getLogger(__name__)

# IBKR Flex Web Service v3 entry point. GetStatement's URL is returned by
# SendRequest (we don't hardcode it).
FLEX_SEND_REQUEST_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest"
_FLEX_VERSION = "3"
# IBKR returns ErrorCode 1019 while a statement is still generating — poll on it.
_GENERATING_CODE = "1019"

# Persistent accumulation of all lots ever seen (gitignored cache/, survives deploys).
LOTS_CACHE_PATH = Path("cache/ibkr_flex_lots.json")
# Drop manually-exported Flex XML statements here to backfill older years.
FLEX_FILE_DIR = Path("cache/ibkr_flex")

# assetCategory values we never treat as a realized-gain disposal. Everything else
# carrying a realized P&L + open date is included (we don't want to silently drop a
# legitimate gain by over-filtering the category whitelist).
_SKIP_ASSET_CATEGORIES = {"CASH"}


class IbkrFlexError(RuntimeError):
    """Raised on any IBKR Flex Web Service failure (bad token, query, or timeout)."""


def _f(value: object) -> float:
    """Parse a Flex numeric attribute to float; blank/None/garbage → 0.0."""
    if value in (None, ""):
        return 0.0
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _parse_flex_date(value: str | None) -> date | None:
    """Parse an IBKR Flex date/datetime to a ``date``.

    Handles the formats IBKR emits: ``YYYYMMDD``, ``YYYYMMDD;HHMMSS``,
    ``YYYY-MM-DD``, and ``YYYYMMDD;HH:MM:SS`` — we only need the date portion, so
    we take the leading 8 digits after stripping separators.
    """
    if not value:
        return None
    head = value.strip().split(";")[0].split(" ")[0].replace("-", "")
    if len(head) >= 8 and head[:8].isdigit():
        return date(int(head[:4]), int(head[4:6]), int(head[6:8]))
    return None


def _tag(e: ET.Element) -> str:
    """Local element tag, namespace-stripped."""
    return e.tag.split("}")[-1]


def parse_realized_lots(xml_text: str) -> list[tuple[str, RealizedGain]]:
    """Parse closed-lot realized gains from a Flex Query XML response.

    Closed-lot detail emits ``<Lot>`` rows (verified against a live IBKR statement:
    ``proceeds`` blank, ``cost`` + ``fifoPnlRealized`` populated). Some query configs
    instead emit ``<Trade levelOfDetail="CLOSED_LOT">`` (proceeds populated). We prefer
    ``<Lot>``, then closed-lot trades, then any realized trade row — never mixing two,
    so a single disposal is counted once. Returns ``(account_id, RealizedGain)`` pairs.

    ``RealizedGain.gain`` is made to reproduce IBKR's authoritative (commission-inclusive)
    ``fifoPnlRealized`` exactly — from ``proceeds`` when present, else by deriving
    proceeds from ``cost`` — and the short/long-term split falls out of ``long_term``.
    """
    root = ET.fromstring(xml_text)
    nodes = [e for e in root.iter() if _tag(e) in ("Lot", "Trade")]
    lots = [n for n in nodes if _tag(n) == "Lot"]
    closed_trades = [n for n in nodes if _tag(n) == "Trade" and (n.get("levelOfDetail") or "").upper() == "CLOSED_LOT"]
    fallback = [n for n in nodes if _tag(n) == "Trade" and n.get("fifoPnlRealized") and n.get("openDateTime")]
    rows = lots or closed_trades or fallback

    out: list[tuple[str, RealizedGain]] = []
    for t in rows:
        if (t.get("assetCategory") or "").upper() in _SKIP_ASSET_CATEGORIES:
            continue
        open_date = _parse_flex_date(t.get("openDateTime") or t.get("holdingPeriodDateTime"))
        close_date = _parse_flex_date(t.get("dateTime") or t.get("tradeDate"))
        if open_date is None or close_date is None:
            continue
        fifo_pnl = _f(t.get("fifoPnlRealized"))
        proceeds_attr = t.get("proceeds")
        if proceeds_attr not in (None, ""):
            proceeds = _f(proceeds_attr)
            cost_basis = proceeds - fifo_pnl
        else:
            # <Lot> rows leave proceeds blank but populate cost; derive proceeds so
            # gain == IBKR's fifoPnlRealized.
            cost_basis = abs(_f(t.get("cost")))
            proceeds = cost_basis + fifo_pnl
        out.append(
            (
                t.get("accountId", "") or "",
                RealizedGain(
                    ticker=t.get("symbol", "") or "",
                    open_date=open_date,
                    close_date=close_date,
                    quantity=abs(_f(t.get("quantity"))),
                    proceeds=proceeds,
                    cost_basis=cost_basis,
                ),
            )
        )
    return out


def _http_get(url: str, params: dict[str, str], opener=urllib.request.urlopen) -> str:
    """GET ``url?params`` and return the decoded body. ``opener`` is injectable for tests."""
    full = url + "?" + urllib.parse.urlencode(params)
    with opener(full, timeout=30) as resp:  # noqa: S310 — fixed IBKR https endpoint
        return resp.read().decode("utf-8")


def fetch_flex_xml(
    token: str,
    query_id: str,
    *,
    poll_attempts: int = 6,
    poll_wait: float = 5.0,
    opener=urllib.request.urlopen,
    sleep=time.sleep,
) -> str:
    """Run the IBKR Flex Web Service v3 two-step and return the statement XML.

    SendRequest → reference code; then poll GetStatement until the statement is
    generated (IBKR returns ErrorCode 1019 while still generating). Raises
    ``IbkrFlexError`` on any terminal failure or if it's not ready after
    ``poll_attempts`` — fail-loud so the caller records the error on a surface.
    """
    send = _http_get(FLEX_SEND_REQUEST_URL, {"t": token, "q": query_id, "v": _FLEX_VERSION}, opener)
    r1 = ET.fromstring(send)
    if (r1.findtext("Status") or "").strip() != "Success":
        raise IbkrFlexError(f"Flex SendRequest failed: {r1.findtext('ErrorCode')} {r1.findtext('ErrorMessage')}")
    reference = (r1.findtext("ReferenceCode") or "").strip()
    get_url = (r1.findtext("Url") or "").strip()
    if not reference or not get_url:
        raise IbkrFlexError("Flex SendRequest returned no reference code / URL")

    for attempt in range(poll_attempts):
        body = _http_get(get_url, {"t": token, "q": reference, "v": _FLEX_VERSION}, opener)
        root = ET.fromstring(body)
        if root.tag.split("}")[-1] == "FlexQueryResponse":
            return body
        # Not the statement yet — a FlexStatementResponse with a status/error.
        code = (root.findtext("ErrorCode") or "").strip()
        if (root.findtext("Status") or "").strip() == "Warn" and code == _GENERATING_CODE:
            if attempt < poll_attempts - 1:
                sleep(poll_wait)
            continue
        raise IbkrFlexError(f"Flex GetStatement failed: {code} {root.findtext('ErrorMessage')}")
    raise IbkrFlexError(f"Flex statement not ready after {poll_attempts} polls")


def load_flex_files(file_dir: Path = FLEX_FILE_DIR) -> list[tuple[str, RealizedGain]]:
    """Parse every ``*.xml`` under ``file_dir`` into realized lots (best-effort per file).

    A single unreadable/malformed statement is logged and skipped (the surviving
    files + the auto-fetch path still populate) — these are operator-dropped backfill
    statements, and one bad file must not blank the whole realized view.
    """
    if not file_dir.exists():
        return []
    lots: list[tuple[str, RealizedGain]] = []
    for path in sorted(file_dir.glob("*.xml")):
        try:
            lots.extend(parse_realized_lots(path.read_text()))
        except (OSError, ET.ParseError) as e:
            # (a) malformed/unreadable backfill file; (c) recorded via WARN log +
            # the other files/auto-fetch still render — never blanks the section.
            logger.warning("Skipping unparseable Flex file %s: %s", path, e)
    return lots


def _lot_key(account_id: str, rg: RealizedGain) -> str:
    """Stable identity for a closed lot, for cross-fetch dedup in the union cache."""
    return f"{account_id}|{rg.ticker}|{rg.open_date}|{rg.close_date}|{rg.quantity}|{rg.proceeds}"


def _load_cached_lots(cache_path: Path = LOTS_CACHE_PATH) -> list[tuple[str, RealizedGain]]:
    """Load the accumulated lots from the union cache; ``[]`` if absent/corrupt."""
    if not cache_path.exists():
        return []
    try:
        rows = json.loads(cache_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Could not read IBKR lots cache %s: %s", cache_path, e)
        return []
    out: list[tuple[str, RealizedGain]] = []
    for r in rows if isinstance(rows, list) else []:
        try:
            out.append(
                (
                    r.get("account_id", ""),
                    RealizedGain(
                        ticker=r["ticker"],
                        open_date=date.fromisoformat(r["open_date"]),
                        close_date=date.fromisoformat(r["close_date"]),
                        quantity=float(r["quantity"]),
                        proceeds=float(r["proceeds"]),
                        cost_basis=float(r["cost_basis"]),
                    ),
                )
            )
        except (KeyError, ValueError, TypeError) as e:
            logger.warning("Skipping malformed cached IBKR lot %r: %s", r, e)
    return out


def _save_lots(lots: list[tuple[str, RealizedGain]], cache_path: Path = LOTS_CACHE_PATH) -> None:
    """Persist the accumulated lots to the union cache."""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = [
        {
            "account_id": acct,
            "ticker": rg.ticker,
            "open_date": rg.open_date.isoformat(),
            "close_date": rg.close_date.isoformat(),
            "quantity": rg.quantity,
            "proceeds": rg.proceeds,
            "cost_basis": rg.cost_basis,
        }
        for acct, rg in lots
    ]
    cache_path.write_text(json.dumps(payload, indent=2))


def _union(
    existing: list[tuple[str, RealizedGain]], new: list[tuple[str, RealizedGain]]
) -> list[tuple[str, RealizedGain]]:
    """Merge two lot lists, deduped by ``_lot_key`` (first occurrence wins)."""
    merged: dict[str, tuple[str, RealizedGain]] = {}
    for acct, rg in [*existing, *new]:
        merged.setdefault(_lot_key(acct, rg), (acct, rg))
    return list(merged.values())


def get_realized_lots(
    token: str | None = None,
    query_id: str | None = None,
    *,
    file_dir: Path = FLEX_FILE_DIR,
    cache_path: Path = LOTS_CACHE_PATH,
    opener=urllib.request.urlopen,
) -> tuple[list[tuple[str, RealizedGain]], str | None]:
    """Return ``(all_accumulated_lots, error)`` — the realized lots for the Tax page.

    Best-effort: starts from the persistent union cache, ingests any dropped backfill
    files, and (when a token + query id are configured) auto-fetches the rolling Flex
    window. All three are unioned and re-persisted, so a fetch failure degrades to the
    last good cache rather than losing data. ``error`` is a human-readable string when
    the live fetch failed (so the page can record it via ``st.warning``), else None.
    """
    accumulated = _load_cached_lots(cache_path)
    new = load_flex_files(file_dir)

    error: str | None = None
    if token and query_id:
        try:
            new = new + parse_realized_lots(fetch_flex_xml(token, query_id, opener=opener))
        except (IbkrFlexError, ET.ParseError, OSError) as e:
            # (a) transient Flex API / network / token failure; (c) recorded here as
            # the returned `error` → surfaced as st.warning on the Tax page, while the
            # accumulated-cache lots still render. Never a silent blank.
            logger.warning("IBKR Flex auto-fetch failed: %s", e)
            error = str(e)

    merged = _union(accumulated, new)
    if len(merged) != len(accumulated):
        _save_lots(merged, cache_path)
    return merged, error
