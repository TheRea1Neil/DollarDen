"""Sign convention and money conversion.

These are pure functions, so they test without a database or network. They
cover the two traps called out in docs/03 and app/plaid/sync.py.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.plaid.sync import split_amount, to_cents


class TestToCents:
    @pytest.mark.parametrize(
        "amount,expected",
        [
            (0, 0), (1, 100), (0.01, 1), (19.99, 1999), ("19.99", 1999),
            (1234.56, 123456), (-42.10, -4210), (0.1, 10), (0.2, 20), (0.3, 30),
        ],
    )
    def test_known_values(self, amount, expected) -> None:
        assert to_cents(amount) == expected

    def test_float_repr_does_not_lose_a_cent(self) -> None:
        """19.99 * 100 is 1998.9999... in binary float; int() of that is 1998."""
        assert int(19.99 * 100) == 1998       # the bug, demonstrated
        assert to_cents(19.99) == 1999        # what we actually do

    def test_decimal_input(self) -> None:
        assert to_cents(Decimal("19.99")) == 1999


class TestSignConvention:
    """Plaid: POSITIVE amount == money LEAVING the account."""

    def test_positive_is_an_outflow(self) -> None:
        out, inflow = split_amount(42.10)
        assert (out, inflow) == (4210, 0)

    def test_negative_is_an_inflow(self) -> None:
        """A paycheck deposit arrives as a negative Plaid amount."""
        out, inflow = split_amount(-2000.00)
        assert (out, inflow) == (0, 200000)

    def test_zero(self) -> None:
        assert split_amount(0) == (0, 0)

    @given(amount=st.decimals(min_value=Decimal("-99999.99"),
                              max_value=Decimal("99999.99"), places=2))
    def test_both_components_are_always_non_negative(self, amount: Decimal) -> None:
        out, inflow = split_amount(amount)
        assert out >= 0 and inflow >= 0

    @given(amount=st.decimals(min_value=Decimal("-99999.99"),
                              max_value=Decimal("99999.99"), places=2))
    def test_exactly_one_side_is_populated(self, amount: Decimal) -> None:
        out, inflow = split_amount(amount)
        assert out == 0 or inflow == 0

    @given(amount=st.decimals(min_value=Decimal("-99999.99"),
                              max_value=Decimal("99999.99"), places=2))
    def test_round_trips_to_the_original_magnitude(self, amount: Decimal) -> None:
        out, inflow = split_amount(amount)
        assert out - inflow == to_cents(amount)
