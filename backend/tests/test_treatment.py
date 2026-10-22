"""Credit card payments must not double-count spending.

The failure this prevents: buy $42 of groceries on a card (Food draws down
$42), then pay the card $500. If the payment also allocated, the same
spending is counted twice and every category is wrong.
"""
from __future__ import annotations

import pytest

from app.budget.treatment import Treatment, classify


def c(**kw):
    base = dict(
        pfc_primary=None, pfc_detailed=None, name="", merchant_name=None,
        account_type="depository", outflow_cents=0, inflow_cents=0,
    )
    base.update(kw)
    return classify(**base)


class TestCardPayments:
    def test_plaid_detailed_category_is_authoritative(self) -> None:
        assert c(pfc_detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT",
                 name="PAYMENT", outflow_cents=50000) is Treatment.CARD_PAYMENT

    def test_payment_arriving_on_the_card(self) -> None:
        """The credit side of the same payment, seen on the card account."""
        assert c(account_type="credit", inflow_cents=50000,
                 pfc_primary="LOAN_PAYMENTS",
                 name="AUTOMATIC PAYMENT - THANK YOU") is Treatment.CARD_PAYMENT

    def test_payment_leaving_checking(self) -> None:
        assert c(account_type="depository", outflow_cents=50000,
                 pfc_primary="LOAN_PAYMENTS",
                 name="CREDIT CARD 3333 PAYMENT") is Treatment.CARD_PAYMENT

    @pytest.mark.parametrize("name", [
        "AUTOPAY THANK YOU", "Online Payment - Credit Card",
        "PAYMENT - THANK YOU", "EPAY CREDIT CARD",
    ])
    def test_common_payment_descriptions(self, name: str) -> None:
        assert c(account_type="credit", inflow_cents=10000,
                 pfc_primary="LOAN_PAYMENTS", name=name) is Treatment.CARD_PAYMENT


class TestNotPayments:
    def test_a_purchase_on_the_card_is_spending(self) -> None:
        assert c(account_type="credit", outflow_cents=4210,
                 pfc_detailed="FOOD_AND_DRINK_GROCERIES",
                 merchant_name="Smiths") is Treatment.SPEND

    def test_a_refund_to_the_card_still_affects_the_budget(self) -> None:
        """Money back from a merchant belongs in the category it left.

        This is the subtle one: a refund also ARRIVES on the card, so naive
        logic would call it a payment and silently drop it.
        """
        assert c(account_type="credit", inflow_cents=2599,
                 pfc_detailed="GENERAL_MERCHANDISE_ONLINE_MARKETPLACES",
                 merchant_name="Amazon", name="AMAZON REFUND") is Treatment.SPEND

    def test_a_paycheck_is_spending_not_a_transfer(self) -> None:
        assert c(account_type="depository", inflow_cents=200000,
                 pfc_primary="INCOME", name="EMPLOYER PAYROLL") is Treatment.SPEND

    def test_an_ordinary_purchase_is_spending(self) -> None:
        assert c(account_type="depository", outflow_cents=1250,
                 merchant_name="KFC", name="KFC") is Treatment.SPEND

    def test_the_word_payment_alone_is_not_enough(self) -> None:
        """A payment TO a merchant is an expense, not a card payment.

        The matcher is deliberately narrow: a false positive here silently
        removes real spending from the budget.
        """
        assert c(account_type="depository", outflow_cents=8000,
                 merchant_name="State Farm",
                 name="INSURANCE PAYMENT") is Treatment.SPEND


class TestTransfers:
    def test_account_transfer_is_not_spending(self) -> None:
        assert c(pfc_detailed="TRANSFER_OUT_ACCOUNT_TRANSFER",
                 outflow_cents=100000) is Treatment.TRANSFER

    def test_moving_money_to_savings_is_not_spending(self) -> None:
        assert c(pfc_detailed="TRANSFER_IN_SAVINGS",
                 inflow_cents=50000) is Treatment.TRANSFER


class TestTreatmentFlags:
    def test_only_spend_affects_the_budget(self) -> None:
        assert Treatment.SPEND.affects_budget is True
        for t in (Treatment.CARD_PAYMENT, Treatment.TRANSFER, Treatment.IGNORED):
            assert t.affects_budget is False, t


class TestEmbeddedAccountNumbers:
    """Real descriptions put the account number between the words."""

    @pytest.mark.parametrize("name", [
        "CREDIT CARD 3333 PAYMENT",
        "CREDIT CARD 3333 PAYMENT *",
        "Credit Card ending 4417 Payment",
        "PAYMENT TO CREDIT CARD 9999",
        "VISA 1234 PAYMENT",
    ])
    def test_matched_despite_digits_in_the_middle(self, name: str) -> None:
        assert c(account_type="depository", outflow_cents=50000,
                 pfc_primary="LOAN_PAYMENTS", name=name) is Treatment.CARD_PAYMENT


class TestOtherLoansStayInTheBudget:
    """LOAN_PAYMENTS also covers real expenses, which must NOT be excluded."""

    @pytest.mark.parametrize("detailed,name", [
        ("LOAN_PAYMENTS_MORTGAGE_PAYMENT", "WELLS FARGO MORTGAGE"),
        ("LOAN_PAYMENTS_CAR_PAYMENT", "TOYOTA FINANCIAL"),
        ("LOAN_PAYMENTS_STUDENT_LOAN_PAYMENT", "NELNET STUDENT LOAN"),
        ("LOAN_PAYMENTS_PERSONAL_LOAN_PAYMENT", "SOFI LOAN"),
    ])
    def test_real_loan_payments_are_spending(self, detailed: str, name: str) -> None:
        assert c(account_type="depository", outflow_cents=120000,
                 pfc_primary="LOAN_PAYMENTS", pfc_detailed=detailed,
                 name=name) is Treatment.SPEND
