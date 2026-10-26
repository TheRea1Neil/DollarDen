"""Reading a receipt's STRUCTURE deterministically, before the model sees it.

A receipt carries its own checksum. It prints a subtotal, a tax line, a
total, and often a count of items sold. Those numbers are findable with
a regex, they are exact, and they are the only way to know whether a
parse is right.

Asking the model to pick them out alongside the items is what went
wrong on the first real receipt. Given a Walmart till slip it returned
TAX2, SUBTOTAL and VISA TEND as things that were bought, and read the
card line

    VISA CREDIT- 4781 TG 0 APPR#O2341D

as an item costing MINUS $4,781.00 -- the last four digits of the card,
with the dash in front of them taken for a negative sign. The lines then
summed to -$4,713.81 against a $22.25 receipt.

That is not a failure of transcription. The OCR text was almost
perfect. It is a failure of JUDGEMENT about which lines are purchases,
and judgement over a fixed, known vocabulary of till-slip terms is
exactly what a regex does better than a language model. Same division as
ADR 0006 and ADR 0009: deterministic code decides structure, the model
is left the part that genuinely needs reading.

So this module runs FIRST and hands the model only the lines that could
plausibly be items, plus the totals to hit.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# A money amount as printed: 1.16, 22.25, 1,234.56. Requires the decimal
# part -- a bare integer on a till slip is far more often a quantity, a
# store number or part of a barcode than a price.
MONEY = re.compile(r"(?<![\d.])(\d{1,3}(?:,\d{3})*\.\d{2})(?![\d])")

# Which lines are structure rather than purchases.
#
# The first version matched these keywords ANYWHERE in a line, which was
# over-fitted to one Walmart slip. Measured against realistic grocery
# names it deleted 8 items in 17 -- CHICKEN TENDER, COUNT CHOCULA, OIL
# CHANGE, BATCH COOKIE DOUGH, CREDIT UNION FEE. Silently dropping a
# purchase is a worse failure than the one this module was written to
# fix, so the rules below are built on two observations instead.
#
# ONE: almost all till-slip noise carries no money at all.
#
#   VISA CREDIT- 4781 TG 0 APPR#O2341D
#   REF # U00TBGO48447  TRANS ID- 386298068855718
#   AID A0000000031010 / TERMINAL # 21090852 / VALIDATION - 9V9K
#
# None of those contain an amount with a decimal point, so the
# no-amount rule removes them without any keyword needing to fire. That
# is what actually caught the card line, and it generalises to every
# EMV terminal rather than to Walmart.
#
# TWO: the handful that DO carry money are label-plus-amount and
# nothing else. "SUBTOTAL  21.60" is the keyword and the figure. A real
# purchase has a description, usually a product code, and often a tax
# flag, so its residue -- the line with the amounts removed -- is
# longer and does not START with the keyword.
#
# So a line with money is structural only when its residue is SHORT and
# matches a tight, anchored pattern. Everything else is a purchase.
MAX_LABEL_WORDS = 3

_CARD = r"visa|mastercard|master\s*card|amex|american\s+express|discover|" \
        r"debit|credit|eftpos|interac|apple\s*pay|google\s*pay|cash"

# Anchored at both ends wherever possible: "CREDIT UNION FEE" must not
# look like a card line, and "CHANGE PURSE" must not look like change
# given.
_LABEL_PATTERNS: tuple[tuple[str, str], ...] = (
    (rf"^sub\s*-?\s*total\b", "subtotal"),
    (rf"^(grand\s+)?total(\s+(purchase|sale|due|amount))?$", "total"),
    (rf"^(amount\s+)?due$", "total"),
    (rf"^(sales\s+)?tax\d*\b", "tax"),
    (rf"^(vat|gst|hst|pst)\d*\b", "tax"),
    (rf"^change(\s+due)?$", "change"),
    (rf"^({_CARD})(\s+(card|tend|tender|payment|sale|purchase|charge))?$",
     "payment"),
    (rf"^(tend|tender)(\s+({_CARD}))?$", "tender"),
    (rf"^balance(\s+due)?$", "total"),
)
_LABEL_RE = [(re.compile(p, re.I), name) for p, name in _LABEL_PATTERNS]

# Matched against the WHOLE line, for noise that can still carry a
# decimal number. Kept deliberately small.
_WHOLE_LINE: tuple[tuple[str, str], ...] = (
    (r"^#?\s*items?\s+sold\b", "item_count"),
    (r"^total\s+items?\b", "item_count"),
    (r"^\s*\*", "slip_note"),
    (r"^\s*\d{1,2}[/-]\d{1,2}[/-]\d{2,4}", "timestamp"),
    (r"^\s*[\d\s-]{7,}\s*$", "digits_only"),
    (r"survey|www\.|\.com|thank you", "store_header"),
)
_WHOLE_LINE_RE = [(re.compile(p, re.I), name) for p, name in _WHOLE_LINE]

# Trailing single-letter flags Walmart and others print after a price
# (F = food-stamp eligible, R / T / N = tax class).
_TRAILING_FLAG = re.compile(r"\b[A-Z]\b\s*$")

# A line carrying nothing but a price (and maybe a tax flag). On a
# correctly-read receipt these do not occur: a price belongs beside its
# item. One appearing means the scan split a row, which is worth saying
# out loud rather than quietly dropping.
_ORPHAN_PRICE = re.compile(r"^\s*\$?\d{1,3}(?:,\d{3})*\.\d{2}\s*[A-Z]{0,2}\s*$")

_LABELLED = {
    "subtotal": re.compile(r"\bsub[\s-]?total\b", re.I),
    "total": re.compile(r"\btotal\b", re.I),
    "tax": re.compile(r"\btax\s*\d?\b|\bsales tax\b", re.I),
}
_ITEM_COUNT = re.compile(r"#?\s*items?\s+sold\s*:?\s*(\d{1,3})", re.I)


def to_cents(s: str) -> int:
    """'22.25' -> 2225. Via the string, never a float (ADR 0002)."""
    whole, _, frac = s.replace(",", "").partition(".")
    return int(whole or 0) * 100 + int((frac or "0").ljust(2, "0")[:2])


@dataclass
class ReceiptStructure:
    """What the till slip states about itself."""

    subtotal_cents: int | None = None
    tax_cents: int | None = None
    total_cents: int | None = None
    item_count: int | None = None
    # Lines that could be purchases, in order.
    item_lines: list[str] = field(default_factory=list)
    # (line, why) for everything removed, so a wrong drop is findable.
    dropped: list[tuple[str, str]] = field(default_factory=list)
    # Rows the OCR split: a price with no item beside it.
    orphan_prices: list[int] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "subtotal_cents": self.subtotal_cents,
            "tax_cents": self.tax_cents,
            "total_cents": self.total_cents,
            "item_count": self.item_count,
            "item_lines": self.item_lines,
            "dropped": [{"line": l, "why": w} for l, w in self.dropped],
            "orphan_prices": self.orphan_prices,
        }


def _last_money(line: str) -> int | None:
    """The rightmost amount on a line -- on a till slip, the price column."""
    found = MONEY.findall(line)
    return to_cents(found[-1]) if found else None


def residue(line: str) -> str:
    """The line with its money amounts and tax flags taken out.

    What is left of "SUBTOTAL  21.60" is "SUBTOTAL". What is left of
    "CHICKEN TENDER 078742011111 F 5.99 R" is still most of a product
    line. That difference is what separates a label from a purchase.
    """
    out = MONEY.sub(" ", line)
    out = _TRAILING_FLAG.sub(" ", out)
    return re.sub(r"\s+", " ", out).strip(" .:-")


def classify(line: str) -> str | None:
    """Why this line is not a purchase, or None if it might be one."""
    for pattern, name in _WHOLE_LINE_RE:
        if pattern.search(line):
            return name

    body = residue(line)
    # A label is short. Anything with a description, a product code and
    # a flag is a purchase, whatever words it happens to contain.
    if len(body.split()) > MAX_LABEL_WORDS:
        return None

    for pattern, name in _LABEL_RE:
        if pattern.search(body):
            return name
    return None


def extract(text: str) -> ReceiptStructure:
    """Split a receipt into its stated totals and its candidate items."""
    out = ReceiptStructure()

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue

        # Pull the stated figures out before dropping the line that
        # carries them. A labelled total is worth far more than anything
        # the model could infer.
        for key, pattern in _LABELLED.items():
            if pattern.search(line):
                amount = _last_money(line)
                if amount is None:
                    continue
                if key == "subtotal" and out.subtotal_cents is None:
                    out.subtotal_cents = amount
                elif key == "tax" and out.tax_cents is None:
                    out.tax_cents = amount
                elif key == "total" and "sub" not in line.lower():
                    # "TOTAL 22.25" and later "22.25 TOTAL PURCHASE" are
                    # the same figure stated twice; keep the first.
                    if out.total_cents is None:
                        out.total_cents = amount

        m = _ITEM_COUNT.search(line)
        if m and out.item_count is None:
            out.item_count = int(m.group(1))

        why = classify(line)
        if why is not None:
            out.dropped.append((line, why))
            continue

        if _ORPHAN_PRICE.match(line):
            amount = _last_money(line)
            if amount is not None:
                out.orphan_prices.append(amount)
            out.dropped.append((line, "orphan_price"))
            continue

        # Something with no amount at all is a header, an address or a
        # barcode caption, not a purchase.
        if _last_money(line) is None:
            out.dropped.append((line, "no_amount"))
            continue

        out.item_lines.append(line)

    return out


def reconcile(
    items: list[dict[str, Any]], structure: ReceiptStructure
) -> list[str]:
    """Check parsed items against what the receipt says about itself.

    Returns human-readable notices. Deliberately does not CORRECT
    anything: a silently repaired receipt is one nobody checks, and the
    screen already refuses to submit until the lines add up.
    """
    notices: list[str] = []
    total = sum(int(i.get("amount_cents") or 0) for i in items)

    if structure.subtotal_cents is not None and total != structure.subtotal_cents:
        diff = structure.subtotal_cents - total
        notices.append(
            f"The items come to ${total / 100:,.2f} but the receipt's subtotal "
            f"is ${structure.subtotal_cents / 100:,.2f} "
            f"({'missing' if diff > 0 else 'over by'} ${abs(diff) / 100:,.2f})."
        )

    if structure.item_count is not None and len(items) != structure.item_count:
        notices.append(
            f"The receipt says {structure.item_count} items sold, but "
            f"{len(items)} were read."
        )

    if structure.orphan_prices:
        prices = ", ".join(f"${p / 100:,.2f}" for p in structure.orphan_prices)
        notices.append(
            f"{len(structure.orphan_prices)} price(s) were read with no item "
            f"beside them ({prices}) — the scan split those rows, so check "
            f"which item each belongs to."
        )

    negative = [i for i in items if int(i.get("amount_cents") or 0) < 0]
    if negative:
        # A real discount is negative; a card number read as one is not.
        # Worth a word either way, since the first receipt tested turned
        # "VISA CREDIT- 4781" into -$4,781.00.
        names = ", ".join(str(i.get("description", "?")) for i in negative[:3])
        notices.append(
            f"Negative amount(s) on: {names}. Keep them only if they are "
            f"discounts or returns."
        )

    return notices
