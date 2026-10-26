"""Reading a receipt's structure without a model.

Every case here comes from one real Walmart slip photographed on a
Galaxy S21 (tests/fixtures/walmart-s21-ocr.txt). That scan is kept
verbatim because it is the only honest sample of what ML Kit actually
produces from curled thermal paper -- a hand-written example would be
tidier than reality and would prove nothing.

The card number, terminal and transaction ids in that fixture are
redacted to zeros. The LAYOUT is untouched -- "VISA CREDIT- 0000" still
has the dash-then-four-digits shape that was being read as -$4,781.00,
which is the only part the parser cares about.
"""
from __future__ import annotations

import pathlib

from app.ai.receipt_text import (
    MONEY,
    ReceiptStructure,
    classify,
    extract,
    reconcile,
    residue,
    to_cents,
)

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "walmart-s21-ocr.txt"
REAL = FIXTURE.read_text()


class TestToCents:
    def test_plain(self):
        assert to_cents("22.25") == 2225

    def test_thousands_separator(self):
        assert to_cents("1,234.56") == 123456

    def test_no_float_drift(self):
        """19.99 * 100 is 1998.999... in binary floating point."""
        assert to_cents("19.99") == 1999


class TestClassify:
    """Asserted as kept-or-dropped, not by which rule fired.

    The reason string is a debugging aid and is free to change; what
    must not change is whether a line can become a purchase. Pinning
    the reason made four tests fail when the card line started being
    caught by the no-amount rule instead of by a brand name — a strictly
    better outcome that the tests called a regression.
    """

    def test_the_card_line_is_never_a_purchase(self):
        """The bug this module exists for.

        "VISA CREDIT- 4781" became an item costing -$4,781.00: the last
        four of the card, with the dash read as a minus sign.

        Checked through extract(), not classify(). classify() answers
        only "is this a label"; the card line is removed because it
        carries no money at all, and that rule lives in extract(). The
        guarantee worth testing is the one the caller gets.
        """
        s = extract("WIDGET  9.99\nVISA CREDIT- 4781 TG 0 APPR#O2341D\n")
        assert s.item_lines == ["WIDGET  9.99"]

    def test_totals_tax_tender_and_change_are_dropped(self):
        for line in ("SUBTOTAL  21.60", "TAX2  3.0000 %  0.65",
                     "TOTAL  22.25", "VISA TEND  22.25", "CHANGE DUE  0.00"):
            assert classify(line) is not None, line

    def test_the_labelled_reasons_stay_meaningful(self):
        """A few anchors, so the reasons do not drift into nonsense."""
        assert classify("SUBTOTAL  21.60") == "subtotal"
        assert classify("TAX2  3.0000 %  0.65") == "tax"
        assert classify("TOTAL  22.25") == "total"

    def test_terminal_noise_is_dropped(self):
        for line in (
            "REF # U00TBGO48447  TRANS ID- 386298068855718  WO",
            "VALIDATION - 9V9K",
            "AID A0000000031010",
            "TERMINAL # 21090852",
            "PAYMENT SERVICE-E",
        ):
            assert classify(line) is not None or MONEY.search(line) is None, line

    def test_a_real_item_is_not_structural(self):
        assert classify("GV BFLOR 12  078742237320 F  1.16 R") is None

    def test_an_item_whose_name_contains_a_structural_word_is_safe(self):
        """Guarding against over-eager patterns."""
        assert classify("MANGO CHUNK 078742025500 F  1.52 R") is None
        assert classify("GV WHOLE GAL 078742351860 F  3.42 R") is None


class TestDoesNotEatRealPurchases:
    """The regression that matters most.

    Matching till-slip keywords anywhere in a line was over-fitted to
    one Walmart receipt. Against realistic grocery names it deleted 8
    items in 17 — and a silently deleted purchase is worse than the
    mis-parse this module was written to fix, because the item simply
    is not there to argue with.

    Every name below is one a supermarket could plausibly print.
    """

    KEEP = [
        "CHICKEN TENDER       078742011111 F  5.99 R",   # 'tender'
        "CHKN TENDERS         078742011112 F  7.49 R",
        "COUNT CHOCULA        078742011113 F  4.29 R",   # 'count'
        "OIL CHANGE                          39.99",     # 'change'
        "CHANGE PURSE         078742011117   12.99",
        "TOTAL WINE GIFT CARD                25.00",     # 'total'
        "TOTAL BLUEBERRIES    078742011125 F  3.99 R",
        "TOTINOS PIZZA        078742011114 F  1.00 R",
        "CREDIT UNION FEE                     3.00",     # 'credit'
        "DEBIT CARD HOLDER    078742011116    9.99",     # 'debit'
        "VISALIA ORANGES      078742011115 F  4.50 R",   # 'visa'
        "TAXI FARE                           18.00",     # 'tax'
        "BATCH COOKIE DOUGH   078742011123 F  5.49 R",   # 'batch'
        "CASHEWS              078742011124 F  6.99 R",   # 'cash'
        "TENDERLOIN STEAK     078742011122 F 14.99 R",
        "AUTHENTIC TACOS      078742011118    6.25",     # 'auth'
    ]

    def test_no_real_item_is_dropped(self):
        eaten = [(l, classify(l)) for l in self.KEEP if classify(l) is not None]
        assert eaten == [], f"these purchases would vanish: {eaten}"

    def test_a_long_line_is_never_a_label(self):
        """A description plus a product code plus a flag is a purchase."""
        assert classify("TOTAL WINE GIFT CARD                25.00") is None


class TestStructureAcrossLayouts:
    """The label forms that must still be caught, from several chains."""

    DROP = [
        "SUBTOTAL  21.60",
        "Subtotal        16.50",
        "SUB-TOTAL       16.50",
        "TAX2  3.0000 %  0.65",
        "Sales Tax        1.32",
        "TOTAL  22.25",
        "GRAND TOTAL     99.99",
        "22.25 TOTAL PURCHASE",
        "AMOUNT DUE      45.00",
        "BALANCE DUE      0.00",
        "VISA TEND  22.25",
        "CHANGE DUE  0.00",
        "CASH            50.00",
        "MASTERCARD      31.20",
    ]

    def test_every_label_form_is_caught(self):
        leaked = [l for l in self.DROP if classify(l) is None]
        assert leaked == [], f"these reached the items: {leaked}"

    def test_residue_strips_amounts_and_flags(self):
        assert residue("SUBTOTAL  21.60") == "SUBTOTAL"
        assert residue("CHANGE DUE  0.00") == "CHANGE DUE"
        assert "078742237320" in residue("GV BFLOR 12  078742237320 F  1.16 R")

    def test_card_noise_with_no_amount_needs_no_keyword(self):
        """What actually caught the card line, and it generalises.

        Terminal noise carries no decimal amount, so it is removed by
        the no-amount rule rather than by a brand name — which means it
        works for any EMV terminal, not just the one tested.
        """
        s = extract(
            "WIDGET  9.99\n"
            "REF # U00TBGO48447  TRANS ID- 386298068855718\n"
            "AID A0000000031010\n"
            "TERMINAL # 21090852\n"
        )
        assert s.item_lines == ["WIDGET  9.99"]
        assert {w for _, w in s.dropped} == {"no_amount"}


class TestExtractOnTheRealScan:
    def test_the_stated_figures_are_recovered_exactly(self):
        s = extract(REAL)
        assert s.subtotal_cents == 2160
        assert s.tax_cents == 65
        assert s.total_cents == 2225
        assert s.item_count == 8

    def test_the_card_number_never_reaches_the_items(self):
        s = extract(REAL)
        assert not any("VISA" in l for l in s.item_lines)
        # Asserted by behaviour, not by the digits: the fixture's card
        # number and terminal ids are redacted, and the bug was never
        # about which four digits were printed.
        card = [line for line, _ in s.dropped if "VISA CREDIT" in line]
        assert len(card) == 1, "the card line must be dropped, by some rule"

    def test_totals_never_reach_the_items(self):
        s = extract(REAL)
        joined = " ".join(s.item_lines).upper()
        for word in ("SUBTOTAL", "TOTAL", "TAX2", "TEND", "CHANGE"):
            assert word not in joined, word

    def test_split_rows_are_reported_rather_than_dropped_silently(self):
        """Both stranded prices are real: 4.24 and 3.16 from the slip."""
        s = extract(REAL)
        assert sorted(s.orphan_prices) == [316, 424]

    def test_a_barcode_is_not_mistaken_for_money(self):
        s = extract(REAL)
        # 078742237320 must not become $787,422,373.20 or similar.
        assert all(abs(p) < 100_000 for p in s.orphan_prices)

    def test_every_line_is_accounted_for(self):
        """Nothing vanishes without a recorded reason."""
        s = extract(REAL)
        non_empty = [l.strip() for l in REAL.splitlines() if l.strip()]
        assert len(s.item_lines) + len(s.dropped) == len(non_empty)


class TestReconcile:
    def _structure(self, **kw):
        return ReceiptStructure(
            subtotal_cents=kw.get("subtotal", 2160),
            item_count=kw.get("count", 8),
            orphan_prices=kw.get("orphans", []),
        )

    def test_silent_when_everything_agrees(self):
        items = [{"description": f"i{n}", "amount_cents": 270} for n in range(8)]
        assert reconcile(items, self._structure()) == []

    def test_says_how_much_is_missing(self):
        items = [{"description": "a", "amount_cents": 2000}]
        out = " ".join(reconcile(items, self._structure(count=1)))
        assert "$20.00" in out and "$21.60" in out and "missing $1.60" in out

    def test_counts_disagreeing_is_called_out(self):
        items = [{"description": "a", "amount_cents": 2160}]
        out = " ".join(reconcile(items, self._structure()))
        assert "8 items sold" in out and "1 were read" in out

    def test_stranded_prices_are_surfaced_to_the_user(self):
        items = [{"description": "a", "amount_cents": 2160}]
        out = " ".join(reconcile(items, self._structure(count=1, orphans=[424])))
        assert "$4.24" in out and "split those rows" in out

    def test_a_negative_amount_is_questioned(self):
        items = [{"description": "VISA CREDIT", "amount_cents": -478100}]
        out = " ".join(reconcile(items, self._structure(count=1)))
        assert "VISA CREDIT" in out and "discounts or returns" in out

    def test_reconcile_never_edits_the_items(self):
        """A silently repaired receipt is one nobody checks."""
        items = [{"description": "a", "amount_cents": 1}]
        before = [dict(i) for i in items]
        reconcile(items, self._structure())
        assert items == before


class TestOtherLayouts:
    def test_a_restaurant_slip_with_a_tip(self):
        text = (
            "THE BLUE DOOR\n"
            "Burger          12.00\n"
            "Fries            4.50\n"
            "Subtotal        16.50\n"
            "Sales Tax        1.32\n"
            "Tip              3.00\n"
            "TOTAL           20.82\n"
        )
        s = extract(text)
        assert s.subtotal_cents == 1650
        assert s.tax_cents == 132
        assert s.total_cents == 2082
        assert [l.split()[0] for l in s.item_lines] == ["Burger", "Fries", "Tip"]

    def test_a_discount_line_survives(self):
        """A real negative must not be filtered out with the card noise."""
        text = "WIDGET  9.99\nCOUPON  -2.00\nTOTAL  7.99\n"
        s = extract(text)
        assert any("COUPON" in l for l in s.item_lines)

    def test_empty_input_does_not_explode(self):
        s = extract("")
        assert s.item_lines == [] and s.subtotal_cents is None
