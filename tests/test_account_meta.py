"""Account taxable classification — auto-derive from connector metadata, manual override."""

from __future__ import annotations

from api.db import models
from api.services.account_meta import is_taxable


def _acct(**kw) -> models.Account:
    return models.Account(broker="ibkr_flex", external_id="U1", **kw)


class TestIsTaxable:
    def test_default_taxable_when_unknown(self):
        assert is_taxable(_acct()) is True

    def test_tax_treatment_drives_it(self):
        assert is_taxable(_acct(tax_treatment="tax_deferred")) is False
        assert is_taxable(_acct(tax_treatment="tax_exempt")) is False
        assert is_taxable(_acct(tax_treatment="taxable")) is True

    def test_account_type_keywords(self):
        assert is_taxable(_acct(account_type="Roth IRA")) is False
        assert is_taxable(_acct(account_type="Traditional IRA")) is False
        assert is_taxable(_acct(account_type="401(k)")) is False
        assert is_taxable(_acct(account_type="HSA")) is False
        assert is_taxable(_acct(account_type="Individual Brokerage")) is True

    def test_name_keywords(self):
        assert is_taxable(_acct(name="My Roth")) is False
        assert is_taxable(_acct(name="Joint Taxable")) is True

    def test_override_wins(self):
        # Override beats every inference, in both directions.
        assert is_taxable(_acct(account_type="Roth IRA", taxable_override=True)) is True
        assert is_taxable(_acct(tax_treatment="taxable", taxable_override=False)) is False

    def test_connector_derived_tax_treatment_preferred_over_keyword_inference(self):
        """metron-ops#194: when the connector has already populated tax_treatment (the
        positive-derivation path in ibkr_flex_connector.py / snaptrade.py), that value
        must win even when account_type/name keywords would imply the opposite —
        keyword inference is demoted to a fallback for when tax_treatment is blank,
        not a second vote."""
        # Connector says taxable; a stale/misleading "IRA" substring in the name must
        # NOT flip it to non-taxable via keyword inference.
        assert is_taxable(_acct(tax_treatment="taxable", name="Old IRA Rollover (closed)")) is True
        # Connector says tax_exempt; an account_type with no recognizable keyword at
        # all must still resolve correctly because tax_treatment already decided it.
        assert is_taxable(_acct(tax_treatment="tax_exempt", account_type="Unlabeled Wrapper")) is False

    def test_keyword_inference_is_only_a_fallback_when_tax_treatment_blank(self):
        # No connector-supplied tax_treatment ("" / unset) → keyword inference on
        # account_type/name is the documented last resort.
        assert is_taxable(_acct(tax_treatment="", account_type="Roth IRA")) is False
        assert is_taxable(_acct(tax_treatment=None, name="My Roth")) is False
