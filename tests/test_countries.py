"""Country-of-domicile cache + US-vs-international classification (api.services.countries).

Mirrors the sector service: ``ensure_countries`` lazily fills NULL ``securities.country``
from an injectable source (the data spine by default), idempotently; ``countries_by_symbol``
reads the cache; ``geo_bucket`` buckets a holding US / International / Unclassified.
"""

from __future__ import annotations

from api.db import models
from api.services import countries


def _add(session, symbol, *, yf_symbol=None, country=None):
    sec = models.Security(symbol=symbol, currency="USD", yf_symbol=yf_symbol, country=country)
    session.add(sec)
    session.commit()
    return sec


class TestGeoBucket:
    def test_us_intl_and_unclassified(self):
        assert countries.geo_bucket("United States") == "US"
        assert countries.is_us_domicile("United States") is True
        assert countries.geo_bucket("Hong Kong") == "International"
        assert countries.geo_bucket("Germany") == "International"
        assert countries.geo_bucket(None) == "Unclassified"
        assert countries.is_us_domicile(None) is False

    def test_international_sentinel_buckets_international(self):
        # A broad-international fund (e.g. FTIHX) reclassified via override to the explicit
        # sentinel buckets International, not US (its listing domicile would say US).
        assert countries.geo_bucket(countries.INTERNATIONAL) == "International"
        assert countries.INTERNATIONAL == "International"
        assert countries.is_us_domicile(countries.INTERNATIONAL) is False


class TestEnsureCountries:
    def test_fills_null_from_source_keyed_by_yf_symbol(self, db_session):
        _add(db_session, "AAPL")
        _add(db_session, "1299", yf_symbol="1299.HK")  # foreign listing → yf-suffixed
        src = {"AAPL": "United States", "1299.HK": "Hong Kong"}
        n = countries.ensure_countries(db_session, ["AAPL", "1299"], source=lambda syms: {k: src[k] for k in syms if k in src})
        assert n == 2
        assert countries.countries_by_symbol(db_session, ["AAPL", "1299"]) == {
            "AAPL": "United States", "1299": "Hong Kong",
        }

    def test_idempotent_no_reclassify(self, db_session):
        _add(db_session, "AAPL", country="United States")
        calls = {"n": 0}

        def src(syms):
            calls["n"] += 1
            return {"AAPL": "Canada"}  # would change it — but it's already set

        n = countries.ensure_countries(db_session, ["AAPL"], source=src)
        assert n == 0 and calls["n"] == 0  # no NULL rows → source never called
        assert countries.countries_by_symbol(db_session, ["AAPL"]) == {"AAPL": "United States"}

    def test_unclassifiable_stays_null_not_guessed(self, db_session):
        _add(db_session, "WEIRD")
        n = countries.ensure_countries(db_session, ["WEIRD"], source=lambda syms: {})
        assert n == 0
        assert countries.countries_by_symbol(db_session, ["WEIRD"]) == {"WEIRD": None}
