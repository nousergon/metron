"""'If sold' tax math — the pure domain calculator (metron-ops#208).

Fixture lots for one holding, valued as of 2025-06-01:

  lot 0  2023-01-10  10 sh @ 100   long-term  (873 days)
  lot 1  2024-03-01   5 sh @ 150   long-term  (457 days)
  lot 2  2025-02-01  10 sh @ 200   short-term (120 days)

Invariants: FIFO relieves oldest first; specific-lot relieves exactly the picks; a
partial lot is pro-rated; the ST/LT split follows the holding period; a loss in one term
nets against a gain in the other before any rate is applied; a net loss carries no tax;
the input lots are never mutated.
"""

from __future__ import annotations

from datetime import date

import pytest

from portfolio_analytics.domain.ledger import (
    Lot,
    Transaction,
    TxnType,
    build_ledger,
    relieve_lots,
)
from portfolio_analytics.domain.tax import (
    LONG_TERM,
    SHORT_TERM,
    LotMethod,
    LotPick,
    TaxRates,
    hypothetical_sale,
    net_capital_gains,
    sale_delta,
)

ASOF = date(2025, 6, 1)
RATES = TaxRates(short_term=0.30, long_term=0.15, state=0.05)


def _lots() -> list[Lot]:
    return [
        Lot("AAPL", date(2023, 1, 10), 10.0, 100.0),
        Lot("AAPL", date(2024, 3, 1), 5.0, 150.0),
        Lot("AAPL", date(2025, 2, 1), 10.0, 200.0),
    ]


class TestFifo:
    def test_mixed_st_lt_full_position(self):
        """Lots 0+1 gain long-term (+800, +150); lot 2 loses short-term (−200). The ST
        loss nets against the LT gain before rates apply: taxable LT = 750."""
        sale = hypothetical_sale(_lots(), quantity=25, price=180.0, asof=ASOF, rates=RATES)
        assert sale.method is LotMethod.FIFO
        assert sale.proceeds == pytest.approx(25 * 180.0)
        assert sale.cost_basis == pytest.approx(1000 + 750 + 2000)
        assert sale.gain_lt == pytest.approx(950.0)
        assert sale.gain_st == pytest.approx(-200.0)
        assert sale.gain_total == pytest.approx(750.0)
        assert (sale.taxable_st, sale.taxable_lt) == pytest.approx((0.0, 750.0))
        assert sale.est_tax_st == 0.0
        assert sale.est_tax_lt == pytest.approx(112.5)
        assert sale.est_tax_state == pytest.approx(37.5)
        assert sale.est_tax_total == pytest.approx(150.0)
        assert [s.lot_index for s in sale.lots] == [0, 1, 2]
        assert [s.term for s in sale.lots] == [LONG_TERM, LONG_TERM, SHORT_TERM]
        assert sale.lots[0].holding_days == (ASOF - date(2023, 1, 10)).days

    def test_all_long_term_position(self):
        lots = _lots()[:2]
        sale = hypothetical_sale(lots, quantity=15, price=160.0, asof=ASOF, rates=RATES)
        assert sale.gain_st == 0.0
        assert sale.gain_lt == pytest.approx(600 + 50)
        assert sale.est_tax_lt == pytest.approx(650 * 0.15)
        assert sale.est_tax_state == pytest.approx(650 * 0.05)
        assert all(s.term == LONG_TERM for s in sale.lots)

    def test_partial_quantity_crossing_a_lot_boundary(self):
        """12 shares: all 10 of lot 0, then 2 of lot 1's 5 (pro-rated basis)."""
        sale = hypothetical_sale(_lots(), quantity=12, price=180.0, asof=ASOF, rates=RATES)
        assert [(s.lot_index, s.quantity) for s in sale.lots] == [(0, 10.0), (1, 2.0)]
        assert sale.lots[1].cost_basis == pytest.approx(300.0)
        assert sale.lots[1].gain == pytest.approx(60.0)
        assert sale.gain_lt == pytest.approx(860.0)
        assert sale.gain_st == 0.0

    def test_loss_position_carries_no_tax(self):
        sale = hypothetical_sale(_lots(), quantity=25, price=90.0, asof=ASOF, rates=RATES)
        assert sale.gain_lt == pytest.approx(-100 - 300)
        assert sale.gain_st == pytest.approx(-1100.0)
        assert (sale.taxable_st, sale.taxable_lt) == (0.0, 0.0)
        assert sale.est_tax_total == 0.0

    def test_fifo_orders_by_open_date_not_input_order(self):
        lots = list(reversed(_lots()))  # newest first in the input
        sale = hypothetical_sale(lots, quantity=10, price=180.0, asof=ASOF, rates=RATES)
        # The oldest lot (now index 2 in the input) is relieved first.
        assert [(s.lot_index, s.open_date) for s in sale.lots] == [(2, date(2023, 1, 10))]

    def test_empty_lots_in_the_input_are_skipped(self):
        lots = [Lot("AAPL", date(2022, 1, 1), 0.0, 50.0), *_lots()]
        sale = hypothetical_sale(lots, quantity=10, price=180.0, asof=ASOF, rates=RATES)
        assert [s.lot_index for s in sale.lots] == [1]

    def test_input_lots_are_never_mutated(self):
        lots = _lots()
        hypothetical_sale(lots, quantity=25, price=180.0, asof=ASOF, rates=RATES)
        hypothetical_sale(
            lots, quantity=3, price=180.0, asof=ASOF, rates=RATES,
            method=LotMethod.SPECIFIC, picks=[LotPick(2, 3.0)],
        )
        assert [lot.quantity for lot in lots] == [10.0, 5.0, 10.0]

    def test_quantity_within_float_tolerance_of_held_is_allowed(self):
        sale = hypothetical_sale(_lots(), quantity=25 + 1e-12, price=180.0, asof=ASOF, rates=RATES)
        assert sum(s.quantity for s in sale.lots) == pytest.approx(25.0)


class TestSpecificLot:
    def test_specific_lots_differ_from_fifo_and_delta_is_reported(self):
        """Same 12 shares: FIFO takes lots 0+1 (LT gains); the user's picks take lot 2
        (ST loss) plus 2 of lot 1 (LT gain) — a different split and a different estimate."""
        fifo = hypothetical_sale(_lots(), quantity=12, price=180.0, asof=ASOF, rates=RATES)
        chosen = hypothetical_sale(
            _lots(), quantity=12, price=180.0, asof=ASOF, rates=RATES,
            method=LotMethod.SPECIFIC, picks=[LotPick(2, 10.0), LotPick(1, 2.0)],
        )
        assert chosen.method is LotMethod.SPECIFIC
        assert [(s.lot_index, s.quantity) for s in chosen.lots] == [(2, 10.0), (1, 2.0)]
        assert chosen.gain_st == pytest.approx(-200.0)
        assert chosen.gain_lt == pytest.approx(60.0)
        assert chosen.est_tax_total == 0.0  # net loss after netting
        assert fifo.est_tax_total == pytest.approx(860 * 0.15 + 860 * 0.05)

        delta = sale_delta(chosen, fifo)
        assert delta.cost_basis == pytest.approx(chosen.cost_basis - fifo.cost_basis)
        assert delta.gain_st == pytest.approx(-200.0)
        assert delta.gain_lt == pytest.approx(60.0 - 860.0)
        assert delta.gain_total == pytest.approx(-140.0 - 860.0)
        assert delta.est_tax_total == pytest.approx(-fifo.est_tax_total)
        # Proceeds are identical — only the lots (and so the basis) differ.
        assert chosen.proceeds == pytest.approx(fifo.proceeds)

    def test_specific_partial_lot(self):
        sale = hypothetical_sale(
            _lots(), quantity=4, price=180.0, asof=ASOF, rates=RATES,
            method=LotMethod.SPECIFIC, picks=[LotPick(0, 4.0)],
        )
        assert sale.lots[0].cost_basis == pytest.approx(400.0)
        assert sale.lots[0].cost_per_share == 100.0
        assert sale.gain_lt == pytest.approx(320.0)

    @pytest.mark.parametrize(
        ("picks", "message"),
        [
            (None, "at least one lot"),
            ([], "at least one lot"),
            ([LotPick(7, 1.0)], "does not exist"),
            ([LotPick(-1, 1.0)], "does not exist"),
            ([LotPick(0, 1.0), LotPick(0, 1.0)], "more than once"),
            ([LotPick(0, 0.0)], "greater than zero"),
            ([LotPick(1, 6.0)], "exceeds its"),
            ([LotPick(0, 1.0)], "not the 2"),
        ],
    )
    def test_invalid_picks_fail_loud(self, picks, message):
        with pytest.raises(ValueError, match=message):
            hypothetical_sale(
                _lots(), quantity=2, price=180.0, asof=ASOF, rates=RATES,
                method=LotMethod.SPECIFIC, picks=picks,
            )


class TestValidation:
    @pytest.mark.parametrize(
        ("quantity", "price", "message"),
        [(0, 180.0, "greater than zero"), (-1, 180.0, "greater than zero"),
         (5, -1.0, "must not be negative"), (26, 180.0, "exceeds")],
    )
    def test_bad_inputs(self, quantity, price, message):
        with pytest.raises(ValueError, match=message):
            hypothetical_sale(_lots(), quantity=quantity, price=price, asof=ASOF, rates=RATES)

    def test_zero_price_is_allowed(self):
        sale = hypothetical_sale(_lots(), quantity=10, price=0.0, asof=ASOF, rates=RATES)
        assert sale.proceeds == 0.0 and sale.gain_lt == pytest.approx(-1000.0)

    @pytest.mark.parametrize("field", ["short_term", "long_term", "state"])
    def test_rates_must_be_fractions(self, field):
        kwargs = {"short_term": 0.2, "long_term": 0.1, "state": 0.0, field: 1.5}
        with pytest.raises(ValueError, match=field):
            TaxRates(**kwargs)
        kwargs[field] = -0.1
        with pytest.raises(ValueError, match=field):
            TaxRates(**kwargs)

    def test_state_rate_defaults_to_zero(self):
        assert TaxRates(short_term=0.2, long_term=0.1).state == 0.0


class TestNetting:
    @pytest.mark.parametrize(
        ("st", "lt", "expected"),
        [
            (100.0, 50.0, (100.0, 50.0)),    # both gains — taxed as is
            (-100.0, -50.0, (0.0, 0.0)),     # both losses — nothing taxable
            (-100.0, 250.0, (0.0, 150.0)),   # ST loss absorbs LT gain
            (-300.0, 250.0, (0.0, 0.0)),     # … fully
            (250.0, -100.0, (150.0, 0.0)),   # LT loss absorbs ST gain
            (100.0, -300.0, (0.0, 0.0)),     # … fully
            (0.0, 0.0, (0.0, 0.0)),
        ],
    )
    def test_net_capital_gains(self, st, lt, expected):
        assert net_capital_gains(st, lt) == pytest.approx(expected)

    def test_long_term_loss_nets_against_short_term_gain(self):
        lots = [Lot("X", date(2020, 1, 1), 10.0, 100.0), Lot("X", date(2025, 5, 1), 10.0, 50.0)]
        sale = hypothetical_sale(lots, quantity=20, price=80.0, asof=ASOF, rates=RATES)
        assert sale.gain_lt == pytest.approx(-200.0)
        assert sale.gain_st == pytest.approx(300.0)
        assert sale.taxable_st == pytest.approx(100.0)
        assert sale.est_tax_st == pytest.approx(30.0)
        assert sale.est_tax_state == pytest.approx(5.0)


class TestRelieveLotsSharedWithTheLedger:
    def test_relieve_lots_matches_ledger_fifo(self):
        """The hypothetical and the real ledger share one relief routine — a real SELL of
        12 at 180 realizes exactly what the FIFO hypothetical measures."""
        txns = [
            Transaction(date(2023, 1, 10), TxnType.BUY, "AAPL", 10, 100.0),
            Transaction(date(2024, 3, 1), TxnType.BUY, "AAPL", 5, 150.0),
            Transaction(ASOF, TxnType.SELL, "AAPL", 12, 180.0),
        ]
        realized = build_ledger(txns).realized
        sale = hypothetical_sale(_lots()[:2], quantity=12, price=180.0, asof=ASOF, rates=RATES)
        assert [(r.quantity, r.gain) for r in realized] == pytest.approx([(s.quantity, s.gain) for s in sale.lots])

    def test_relieve_lots_pops_closed_lots_and_stops_when_exhausted(self):
        lots = _lots()
        out = relieve_lots(lots, 100.0, close_date=ASOF, proceeds_per_share=1.0)
        assert len(out) == 3 and lots == []
