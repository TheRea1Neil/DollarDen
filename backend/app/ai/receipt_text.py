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

# Lines that are never a purchase. Ordered most specific first so the
# reason reported is the useful one.
#
# Every entry here earned its place on a real receipt. In particular the
# card-brand rule exists because "VISA CREDIT- 4781" parsed as money.
_STRUCTURAL: tuple[tuple[str, str], ...] = (
    (r"\bsub[\s-]?total\b", "subtotal"),
    (r"\btotal\b", "total"),
    (r"\btax\s*\d?\b|\bsales tax\b|\bvat\b|\bgst\b|\bhst\b", "tax"),
    (r"\bchange\b", "change"),
    (r"\btende?r?\b", "tender"),
    (r"\b(visa|mastercard|master card|amex|american express|discover|"
     r"debit|credit|eftpos|interac|apple pay|google pay)\b", "payment"),
    (r"\bappr(oval)?\s*#|\bauth\b|\bref\s*#|\btrans(action)?\s*id\b|"
     r"\bvalidation\b|\bterminal\s*#|\baid\s+[a-f0-9]{8,}|"
     r"\bpayment service\b|\bmerchant\s*#|\bbatch\b|\barc\b", "terminal"),
    (r"#\s*items?\s+sold|\bitems?\s+sold\b|\bcount\b", "item_count"),
    (r"\b(tc|st|op|te|tr)#", "store_code"),
    (r"\bsignature\b|\bcustomer copy\b|\bmerchant copy\b", "slip_note"),
    (r"survey|feedback|thank you|www\.|\.com|\bmgr\b|\bcashier\b|"
     r"\bstore\s*\d|\bphone\b", "store_header"),
    (r"^\s*\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|^\s*\d{1,2}:\d{2}", "timestamp"),
    (r"^\s*[\d\s-]{7,}\s*$", "digits_only"),
    (r"\breturn policy\b|\bexchange\b|\bsave money\b|\blive better\b", "boilerplate"),
)
_STRUCTURAL_RE = [(re.compile(p, re.I), name) for p, name in _STRUCTURAL]

# A line carrying nothing but a price (and maybe a tax flag). On a
# correctly-read receipt these do not occur: a price belongs beside its
# item. One appearing means the OCR split a row, which is worth saying
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


def classify(line: str) -> str | None:
    """Why this line is not a purchase, or None if it might be one."""
    for pattern, name in _STRUCTURAL_RE:
        if pattern.search(line):
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
