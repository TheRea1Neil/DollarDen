"""How a transaction should affect the budget.

The problem this solves
-----------------------
Spending on a credit card and then paying that card are TWO transactions for
ONE expense:

    1. $42 groceries on the card   -> Food draws down      (the expense)
    2. $500 payment from checking  -> settles the debt     (not an expense)

If the payment also drew down a category, the same money would be counted
twice. So a card payment is a TRANSFER, not spending, and must not allocate.

The consequence is worth stating plainly: category balances represent
COMMITTED money, not cash in the bank. Food shows what is left to spend on
food, whether or not the card bill has been paid yet.
"""
from __future__ import annotations

import re
from enum import StrEnum


class Treatment(StrEnum):
    SPEND = "spend"                # normal: allocates against a category
    CARD_PAYMENT = "card_payment"  # paying a credit card: settles existing debt
    TRANSFER = "transfer"          # between the user's own accounts
    IGNORED = "ignored"            # user chose to exclude it

    @property
    def affects_budget(self) -> bool:
        return self is Treatment.SPEND


# Plaid classifies these itself and is reliable for them.
CARD_PAYMENT_PFC = {
    "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT",
}
TRANSFER_PFC = {
    "TRANSFER_OUT_ACCOUNT_TRANSFER",
    "TRANSFER_IN_ACCOUNT_TRANSFER",
    "TRANSFER_OUT_SAVINGS",
    "TRANSFER_IN_SAVINGS",
}

# Fallback for when Plaid's detailed category is absent or unhelpful.
# Deliberately narrow: a false positive here silently drops a real expense
# from the budget, which is worse than an extra item in the review queue.
_PAYMENT_PHRASES = (
    "autopay",
    "automatic payment",
    "payment - thank you",
    "payment thank you",
    "online payment",
    "epay",
)

# Card payments routinely carry the account number between the words, e.g.
# "CREDIT CARD 3333 PAYMENT", so a literal substring match misses them.
_CARD_PAYMENT_RE = re.compile(
    r"(credit\s*card|card\s*ending|visa|mastercard|amex)\b.{0,24}?\bpayment"
    r"|payment\b.{0,24}?\b(credit\s*card|card\s*ending)",
    re.IGNORECASE,
)


def _looks_like_card_payment(text: str) -> bool:
    return bool(_CARD_PAYMENT_RE.search(text)) or any(
        phrase in text for phrase in _PAYMENT_PHRASES
    )


def classify(
    *,
    pfc_primary: str | None,
    pfc_detailed: str | None,
    name: str,
    merchant_name: str | None,
    account_type: str | None,
    outflow_cents: int,
    inflow_cents: int,
) -> Treatment:
    """Decide how a transaction should be treated. Pure function, easily tested."""
    detailed = (pfc_detailed or "").upper()
    primary = (pfc_primary or "").upper()
    text = f"{merchant_name or ''} {name}".lower()
    is_credit_account = (account_type or "").lower() == "credit"

    # 1. Plaid said so outright.
    if detailed in CARD_PAYMENT_PFC:
        return Treatment.CARD_PAYMENT

    # 2. Money ARRIVING on a credit card is either a payment or a refund.
    #    A refund should still affect the budget -- it puts money back in the
    #    category it came out of -- so only treat it as a payment when the
    #    description or Plaid's category actually says payment.
    if is_credit_account and inflow_cents > 0:
        if primary == "LOAN_PAYMENTS" or _looks_like_card_payment(text):
            return Treatment.CARD_PAYMENT
        return Treatment.SPEND  # a refund

    # 3. Money LEAVING a depository account toward a card.
    #    LOAN_PAYMENTS alone is NOT sufficient here: mortgage, car and
    #    student loan payments share that category and are real expenses
    #    that must stay in the budget. Only a card-specific description
    #    qualifies.
    if not is_credit_account and outflow_cents > 0 and _looks_like_card_payment(text):
        return Treatment.CARD_PAYMENT

    # 4. Transfers between the user's own accounts move money without
    #    spending it.
    if detailed in TRANSFER_PFC:
        return Treatment.TRANSFER

    return Treatment.SPEND


def explain(treatment: Treatment) -> str:
    """User-facing reason, so an excluded transaction is never a mystery."""
    return {
        Treatment.SPEND: "Counted against a category",
        Treatment.CARD_PAYMENT:
            "Credit card payment — not counted, because the purchases it "
            "pays for were already budgeted",
        Treatment.TRANSFER: "Transfer between your own accounts — not spending",
        Treatment.IGNORED: "You excluded this from the budget",
    }[treatment]
