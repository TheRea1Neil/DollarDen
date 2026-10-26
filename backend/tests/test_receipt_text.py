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
    ReceiptStructure,
    classify,
    extract,
    reconcile,
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
    def test_the_card_line_is_payment_not_a_purchase(self):
        """The bug this module exists for.

        "VISA CREDIT- 4781" became an item costing -$4,781.00: the last
        four of the card, with the dash read as a minus sign.
        """
        assert classify("VISA CREDIT- 4781 TG 0 APPR#O2341D") == "payment"

    def test_totals_tax_tender_and_change(self):
        assert classify("SUBTOTAL  21.60") == "subtotal"
        assert classify("TAX2  3.0000 %  0.65") == "tax"
        assert classify("TOTAL  22.25") == "total"
        assert classify("VISA TEND  22.25") == "tender"
        assert classify("CHANGE DUE  0.00") == "change"

    def test_terminal_noise(self):
        for line in (
            "REF # U00TBGO48447  TRANS ID- 386298068855718  WO",
            "VALIDATION - 9V9K",
            "AID A0000000031010",
            "TERMINAL # 21090852",
            "PAYMENT SERVICE-E",
        ):
            assert classify(line) == "terminal", line

    def test_a_real_item_is_not_structural(self):
        assert classify("GV BFLOR 12  078742237320 F  1.16 R") is None

    def test_an_item_whose_name_contains_a_structural_word_is_safe(self):
        """Guarding against over-eager patterns."""
        assert classify("MANGO CHUNK 078742025500 F  1.52 R") is None
        assert classify("GV WHOLE GAL 078742351860 F  3.42 R") is None


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
        card = [why for line, why in s.dropped if "VISA CREDIT" in line]
        assert card == ["payment"]

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
