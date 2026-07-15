"""``tax_treatment_from_account_type`` — the connector-side positive-derivation table
(metron-ops#194). Both IBKR Flex and SnapTrade connectors call this shared helper so
they can't drift on the same broker-reported vocabulary; account_meta.py's keyword
inference is the documented last-resort fallback for whatever this can't resolve.
"""

from __future__ import annotations

from portfolio_analytics.ingestion.schema import tax_treatment_from_account_type


class TestTaxTreatmentFromAccountType:
    def test_taxable_types(self):
        assert tax_treatment_from_account_type("Individual") == "taxable"
        assert tax_treatment_from_account_type("Joint") == "taxable"
        assert tax_treatment_from_account_type("Brokerage") == "taxable"
        assert tax_treatment_from_account_type("Trust") == "taxable"

    def test_tax_deferred_types(self):
        assert tax_treatment_from_account_type("Traditional IRA") == "tax_deferred"
        assert tax_treatment_from_account_type("IRA") == "tax_deferred"
        assert tax_treatment_from_account_type("401(k)") == "tax_deferred"
        assert tax_treatment_from_account_type("403b") == "tax_deferred"
        assert tax_treatment_from_account_type("SEP IRA") == "tax_deferred"

    def test_tax_exempt_types(self):
        assert tax_treatment_from_account_type("Roth IRA") == "tax_exempt"
        assert tax_treatment_from_account_type("Roth 401(k)") == "tax_exempt"
        assert tax_treatment_from_account_type("HSA") == "tax_exempt"
        assert tax_treatment_from_account_type("529") == "tax_exempt"

    def test_roth_ira_prefers_exempt_over_bare_ira_substring(self):
        # "roth ira" contains "ira" — the longest-match-wins rule must pick the more
        # specific "roth ira" (exempt) phrase, not the shorter "ira" (deferred) one.
        assert tax_treatment_from_account_type("Roth IRA - Individual") == "tax_exempt"

    def test_case_insensitive(self):
        assert tax_treatment_from_account_type("roth ira") == "tax_exempt"
        assert tax_treatment_from_account_type("TRADITIONAL IRA") == "tax_deferred"

    def test_unrecognized_type_returns_blank_not_a_guess(self):
        assert tax_treatment_from_account_type("Some Exotic Wrapper") == ""
        assert tax_treatment_from_account_type("") == ""
        assert tax_treatment_from_account_type(None) == ""
