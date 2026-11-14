"""Every cached close and FX rate names the producer object it was read from
(metron-ops-I373, metron-ops-I370).

The spine readers used to hand back a bare ``(date, close)``: the S3 key they read is
overwritten on every producer run, so nothing stored could say WHICH version of the
artifact a value came from, what price basis it declared, or when Metron recorded it. A
NAV revision preserved in ``nav_snapshot_revisions`` could therefore name its inputs only
by (security, day). Now each ``ClosePoint`` from the spine carries an ``InputSource``
(object location, S3 version id / ETag, adjustment basis), ``price_bars`` and ``fx_rates``
store it with ``recorded_at``, and a superseded value keeps its own lineage in
``market_input_revisions``.

Synthetic prices, rates and object versions only.
"""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import MagicMock

from sqlalchemy import select

from api.db import models
from api.services import fx as fx_service
from api.services import prices as price_service
from portfolio_analytics.prices import ClosePoint, InputSource, spine_source

D1, D2 = date(2024, 5, 28), date(2024, 5, 29)
BUCKET = "alpha-engine-research"


def _s3_with(objects: dict[str, tuple[dict, str | None, str | None]]) -> MagicMock:
    """A MagicMock S3 serving ``key -> (json, VersionId, ETag)``."""
    s3 = MagicMock()

    def _get(*, Bucket, Key):  # noqa: N803 - boto3 kwarg names
        if Key not in objects:
            raise Exception(f"NoSuchKey: {Key}")
        art, version_id, etag = objects[Key]
        body = MagicMock()
        body.read.return_value = json.dumps(art).encode()
        out: dict = {"Body": body}
        if version_id is not None:
            out["VersionId"] = version_id
        if etag is not None:
            out["ETag"] = f'"{etag}"'
        return out

    s3.get_object.side_effect = _get
    return s3


def _history_art(closes, basis="dividend_adjusted"):
    return {"schema_version": 1, "adjustment_basis": basis, "closes": closes}


def _security(session, symbol="AAPL"):
    sec = models.Security(symbol=symbol, currency="USD")
    session.add(sec)
    session.commit()
    return sec


def _src(version: str, basis: str | None = "dividend_adjusted") -> InputSource:
    return InputSource(
        artifact=f"s3://{BUCKET}/market_data/close_history/AAPL.json", version_id=version, adjustment_basis=basis
    )


def _history(points):
    return lambda symbols, start, end: {"AAPL": points} if "AAPL" in symbols else {}


def _latest(points):
    def src(symbols, *, source=None):
        return {s: points[s] for s in symbols if s in points}

    return src


def _bar(session, day=D1):
    return session.scalars(select(models.PriceBar).where(models.PriceBar.bar_date == day)).one()


def _revisions(session):
    return session.scalars(
        select(models.MarketInputRevision).order_by(models.MarketInputRevision.superseded_at)
    ).all()


class TestSpineReaderLineage:
    def test_history_point_names_the_object_version_and_basis_it_was_read_from(self):
        key = "market_data/close_history/AAPL.json"
        s3 = _s3_with({key: (_history_art([["2024-05-28", 100.0], ["2024-05-29", 101.0]]), "v-7", "abc123")})
        points = spine_source.spine_close_history(["AAPL"], D1, D2, s3=s3)["AAPL"]
        assert [p.source for p in points] == [
            InputSource(
                artifact=f"s3://{BUCKET}/{key}", version_id="v-7", etag="abc123", adjustment_basis="dividend_adjusted"
            )
        ] * 2
        assert points[0].source is not None
        assert points[0].source.ref == f"s3://{BUCKET}/{key}?versionId=v-7"

    def test_unversioned_object_falls_back_to_its_content_digest(self):
        """S3 answers VersionId "null" for an object written while versioning was off: that
        names no version, so the ETag (the content digest) identifies the bytes instead."""
        key = "market_data/fx_history/HKD.json"
        s3 = _s3_with({key: ({"schema_version": 1, "rates": [["2024-05-28", 0.128]]}, "null", "d1g3st")})
        (point,) = spine_source.spine_close_history(["HKDUSD=X"], D1, D2, s3=s3)["HKDUSD=X"]
        assert point.source is not None
        assert point.source.version_id is None
        assert point.source.adjustment_basis is None  # FX declares no price basis
        assert point.source.ref == f"s3://{BUCKET}/{key}?etag=d1g3st"

    def test_latest_close_and_rate_name_their_own_artifacts(self):
        s3 = _s3_with({
            spine_source.CLOSES_LATEST_KEY: (
                {"schema_version": 1, "closes": {"AAPL": {"close": 101.0, "currency": "USD", "bar_date": "2024-05-29"}}},
                "c-1", None,
            ),
            spine_source.FX_LATEST_KEY: (
                {"schema_version": 1, "base": "USD", "as_of": "2024-05-29", "rates": {"HKD": 0.128}}, "f-1", None,
            ),
        })
        out = spine_source.spine_latest_closes(["AAPL", "HKDUSD=X"], s3=s3)
        assert out["AAPL"].source is not None and out["HKDUSD=X"].source is not None
        assert out["AAPL"].source.ref == f"s3://{BUCKET}/{spine_source.CLOSES_LATEST_KEY}?versionId=c-1"
        assert out["HKDUSD=X"].source.ref == f"s3://{BUCKET}/{spine_source.FX_LATEST_KEY}?versionId=f-1"

    def test_lineage_does_not_change_point_equality(self):
        """The same close on the same day is the same observation whichever object version
        carried it; lineage is metadata, so existing comparisons keep their meaning."""
        assert ClosePoint(D1, 100.0, source=_src("a")) == ClosePoint(D1, 100.0)


class TestBackfillLineage:
    def test_backfilled_bar_stores_its_lineage(self, db_session):
        _security(db_session)
        price_service.backfill_prices(db_session, ["AAPL"], D1, D1, source=_history([ClosePoint(D1, 100.0, _src("v1"))]))
        bar = _bar(db_session)
        assert bar.source_ref == _src("v1").ref
        assert bar.adjustment_basis == "dividend_adjusted"
        assert bar.recorded_at is not None

    def test_revised_close_carries_new_lineage_and_the_revision_keeps_the_old(self, db_session):
        _security(db_session)
        price_service.backfill_prices(db_session, ["AAPL"], D1, D1, source=_history([ClosePoint(D1, 100.0, _src("v1"))]))
        first_recorded = _bar(db_session).recorded_at
        price_service.backfill_prices(db_session, ["AAPL"], D1, D1, source=_history([ClosePoint(D1, 95.5, _src("v2"))]))
        db_session.expire_all()
        bar = _bar(db_session)
        assert (float(bar.close), bar.source_ref) == (95.5, _src("v2").ref)
        (rev,) = _revisions(db_session)
        assert (float(rev.value), rev.source_ref, rev.adjustment_basis, rev.recorded_at) == (
            100.0, _src("v1").ref, "dividend_adjusted", first_recorded,
        )

    def test_identical_resend_keeps_the_lineage_of_the_first_read(self, db_session):
        """Three backfills a night re-send every bar from a fresh object version. An
        unchanged close is not a new observation: its lineage and recorded_at stay put."""
        _security(db_session)
        price_service.backfill_prices(db_session, ["AAPL"], D1, D1, source=_history([ClosePoint(D1, 100.0, _src("v1"))]))
        first = _bar(db_session)
        first_ref, first_recorded = first.source_ref, first.recorded_at
        price_service.backfill_prices(db_session, ["AAPL"], D1, D1, source=_history([ClosePoint(D1, 100.0, _src("v9"))]))
        db_session.expire_all()
        bar = _bar(db_session)
        assert (bar.source_ref, bar.recorded_at) == (first_ref, first_recorded)
        assert _revisions(db_session) == []

    def test_a_source_that_cannot_name_its_origin_stores_no_reference(self, db_session):
        _security(db_session)
        price_service.backfill_prices(db_session, ["AAPL"], D1, D1, source=_history([ClosePoint(D1, 100.0)]))
        bar = _bar(db_session)
        assert (bar.source_ref, bar.adjustment_basis) == (None, None)
        assert bar.recorded_at is not None

    def test_spine_read_to_stored_bar_end_to_end(self, db_session):
        _security(db_session)
        key = "market_data/close_history/AAPL.json"
        s3 = _s3_with({key: (_history_art([["2024-05-28", 100.0]]), "v-42", "e")})
        price_service.backfill_prices(
            db_session, ["AAPL"], D1, D1, source=lambda syms, s, e: spine_source.spine_close_history(syms, s, e, s3=s3)
        )
        bar = _bar(db_session)
        assert bar.source_ref == f"s3://{BUCKET}/{key}?versionId=v-42"
        assert bar.adjustment_basis == "dividend_adjusted"


class TestLatestRefreshLineage:
    def test_refreshed_close_moves_lineage_and_preserves_the_old_one(self, db_session):
        _security(db_session)
        price_service.refresh_latest_prices(
            db_session, ["AAPL"], source=_latest({"AAPL": ClosePoint(D1, 100.0, _src("v1", basis=None))})
        )
        first_recorded = _bar(db_session).recorded_at
        # Same close from a newer object: not a revision, lineage unchanged.
        price_service.refresh_latest_prices(
            db_session, ["AAPL"], source=_latest({"AAPL": ClosePoint(D1, 100.0, _src("v2", basis=None))})
        )
        assert _bar(db_session).source_ref == _src("v1").ref
        price_service.refresh_latest_prices(
            db_session, ["AAPL"], source=_latest({"AAPL": ClosePoint(D1, 102.0, _src("v3", basis=None))})
        )
        bar = _bar(db_session)
        assert (float(bar.close), bar.source_ref) == (102.0, _src("v3").ref)
        (rev,) = _revisions(db_session)
        assert (float(rev.value), rev.source_ref, rev.recorded_at) == (100.0, _src("v1").ref, first_recorded)

    def test_deleted_close_keeps_its_lineage(self, db_session):
        _security(db_session)
        price_service.refresh_latest_prices(db_session, ["AAPL"], source=_latest({"AAPL": ClosePoint(D1, 100.0, _src("v1"))}))
        db_session.delete(_bar(db_session))
        db_session.commit()
        (rev,) = _revisions(db_session)
        assert (rev.change, rev.source_ref, rev.adjustment_basis) == ("delete", _src("v1").ref, "dividend_adjusted")


class TestFxLineage:
    @staticmethod
    def _fx_src(version: str) -> InputSource:
        return InputSource(artifact=f"s3://{BUCKET}/{spine_source.FX_LATEST_KEY}", version_id=version)

    def _rate(self, session):
        return session.scalars(select(models.FxRate)).one()

    def test_refreshed_rate_moves_lineage_and_preserves_the_old_one(self, db_session):
        fx_service.refresh_fx_rates(db_session, ["HKD"], source=_latest({"HKDUSD=X": ClosePoint(D1, 0.128, self._fx_src("f1"))}))
        first = self._rate(db_session)
        assert first.source_ref == self._fx_src("f1").ref
        first_recorded = first.recorded_at
        fx_service.refresh_fx_rates(db_session, ["HKD"], source=_latest({"HKDUSD=X": ClosePoint(D1, 0.130, self._fx_src("f2"))}))
        assert self._rate(db_session).source_ref == self._fx_src("f2").ref
        (rev,) = _revisions(db_session)
        assert (rev.kind, float(rev.value), rev.source_ref, rev.adjustment_basis, rev.recorded_at) == (
            "fx", 0.128, self._fx_src("f1").ref, None, first_recorded,
        )

    def test_backfilled_rate_stores_its_lineage(self, db_session):
        src = InputSource(artifact=f"s3://{BUCKET}/market_data/fx_history/HKD.json", etag="e1")
        fx_service.backfill_fx_rates(
            db_session, ["HKD"], D1, D1,
            source=lambda syms, s, e: {"HKDUSD=X": [ClosePoint(D1, 0.128, src)]} if "HKDUSD=X" in syms else {},
        )
        rate = self._rate(db_session)
        assert rate.source_ref == src.ref
        assert rate.recorded_at is not None
