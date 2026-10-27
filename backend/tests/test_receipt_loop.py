"""The scratchpad loop: arithmetic, the remainder, and the guard rails.

Every rule tested here exists because a real receipt broke without it.
The model is stubbed throughout — what matters is that the loop keeps
its promises whatever the model does, including when it misbehaves.
"""
from __future__ import annotations


from app.ai.provider import ChatResult
from app.ai.receipt_loop import (
    MAX_TOOL_CALLS,
    Scratchpad,
    run_loop,
    to_cents,
)

CATS = [("Food & Groceries", "food"), ("Health", "pharmacy")]


class StubModel:
    """Replays a scripted sequence of tool calls."""

    def __init__(self, script):
        self.script = list(script)
        self.seen = []

    async def chat(self, prompt, **kw):
        self.seen.append(kw.get("messages"))
        if not self.script:
            return ChatResult(content="", model="stub")
        calls = self.script.pop(0)
        return ChatResult(content="", model="stub", tool_calls=calls)


def save(items):
    return [{"name": "save_items", "arguments": {"items": items}}]


def fix(i, amt):
    return [{"name": "fix_item", "arguments": {"index": i, "amount": amt}}]


SUBMIT = [{"name": "submit", "arguments": {}}]


class TestToCents:
    def test_dollars_to_cents(self):
        assert to_cents(2.88) == 288
        assert to_cents("$1,234.56") == 123456

    def test_negative_for_a_void(self):
        assert to_cents(-11.74) == -1174

    def test_no_float_drift(self):
        """19.99 * 100 is 1998.999... in binary floating point."""
        assert to_cents(19.99) == 1999

    def test_rubbish_is_none_not_zero(self):
        """Zero would silently swallow a line; None drops it visibly."""
        assert to_cents("banana") is None
        assert to_cents(None) is None


class TestScratchpad:
    def test_sums_and_difference(self):
        pad = Scratchpad(2225)
        out = pad.save([{"description": "a", "amount": 21.60, "category": "Food & Groceries"},
                        {"description": "tax", "amount": 0.65, "category": "Food & Groceries"}])
        assert out["matches"] is True
        assert out["difference"] == "$0.00"

    def test_tells_the_model_which_way_it_is_wrong(self):
        pad = Scratchpad(2225)
        out = pad.save([{"description": "a", "amount": 32.25, "category": "Food & Groceries"}])
        assert out["matches"] is False
        assert "over" in out["hint"]

    def test_fix_out_of_range_is_an_error_not_a_crash(self):
        pad = Scratchpad(1000)
        pad.save([{"description": "a", "amount": 5.00, "category": "Health"}])
        assert "error" in pad.fix(9, 1.0)

    def test_reconcile_adds_one_flagged_line_to_the_dominant_category(self):
        pad = Scratchpad(10530)
        pad.save([
            {"description": "food", "amount": 60.00, "category": "Food & Groceries"},
            {"description": "pills", "amount": 20.00, "category": "Health"},
        ])
        line = pad.reconcile()
        assert line is not None
        assert line.cents == 2530
        assert line.category == "Food & Groceries"
        assert line.needs_review is True
        assert pad.total_cents == 10530

    def test_reconcile_is_silent_when_it_already_balances(self):
        pad = Scratchpad(500)
        pad.save([{"description": "a", "amount": 5.00, "category": "Health"}])
        assert pad.reconcile() is None
        assert len(pad.lines) == 1



class TestLoop:
    async def test_a_clean_read_closes_without_a_remainder(self):
        model = StubModel([
            save([{"description": "milk", "amount": 21.60, "category": "Food & Groceries"},
                  {"description": "tax", "amount": 0.65, "category": "Food & Groceries"}]),
            SUBMIT,
        ])
        r = await run_loop(model, image_b64="x", target_cents=2225, categories=CATS)
        assert r.closed is True and r.submitted is True
        assert r.remainder is None
        assert sum(l.cents for l in r.lines) == 2225

    async def test_a_misread_is_repaired_and_then_closes(self):
        model = StubModel([
            save([{"description": "milk", "amount": 31.60, "category": "Food & Groceries"},
                  {"description": "tax", "amount": 0.65, "category": "Food & Groceries"}]),
            fix(0, 21.60),
            SUBMIT,
        ])
        r = await run_loop(model, image_b64="x", target_cents=2225, categories=CATS)
        assert r.closed is True
        assert r.lines[0].cents == 2160

    async def test_the_result_always_balances_even_when_the_model_gives_up(self):
        """The promise the rest of the app depends on."""
        model = StubModel([
            save([{"description": "milk", "amount": 10.00, "category": "Food & Groceries"}]),
            SUBMIT,
        ])
        r = await run_loop(model, image_b64="x", target_cents=2225, categories=CATS)
        assert r.closed is False
        assert r.remainder is not None and r.remainder.needs_review
        assert sum(l.cents for l in r.lines) == 2225, "must reconcile to the bank"

    async def test_an_oscillating_fixer_is_cut_off(self):
        """Target: it flipped one line between two values for eight calls."""
        model = StubModel([
            save([{"description": "toy", "amount": 1.98, "category": "Food & Groceries"}]),
            fix(0, 19.98), fix(0, 1.98), fix(0, 19.98), fix(0, 1.98),
            fix(0, 19.98), fix(0, 1.98), fix(0, 19.98),
        ])
        r = await run_loop(model, image_b64="x", target_cents=2225, categories=CATS)
        assert r.tool_calls < MAX_TOOL_CALLS, "the repeat guard should stop it early"
        assert sum(l.cents for l in r.lines) == 2225

    async def test_a_runaway_model_is_capped(self):
        model = StubModel([
            save([{"description": f"x{n}", "amount": 1.00, "category": "Health"}])
            for n in range(50)
        ])
        r = await run_loop(model, image_b64="x", target_cents=2225, categories=CATS)
        assert r.tool_calls <= MAX_TOOL_CALLS
        assert sum(l.cents for l in r.lines) == 2225

    async def test_a_model_that_never_calls_a_tool_still_balances(self):
        """It spent its whole budget thinking and said nothing."""
        model = StubModel([])
        r = await run_loop(model, image_b64="x", target_cents=2225, categories=CATS)
        assert len(r.lines) == 1 and r.lines[0].needs_review
        assert r.lines[0].cents == 2225

    async def test_submit_is_refused_while_it_does_not_match(self):
        model = StubModel([
            save([{"description": "a", "amount": 10.00, "category": "Health"}]),
            SUBMIT,
        ])
        r = await run_loop(model, image_b64="x", target_cents=2225, categories=CATS)
        assert r.submitted is False, "submit must not succeed on a mismatch"

    async def test_the_category_split_is_reported(self):
        model = StubModel([
            save([{"description": "food", "amount": 21.00, "category": "Food & Groceries"},
                  {"description": "pills", "amount": 1.25, "category": "Health"}]),
            SUBMIT,
        ])
        r = await run_loop(model, image_b64="x", target_cents=2225, categories=CATS)
        split = {s["category"]: s["amount_cents"] for s in r.as_dict()["category_split"]}
        assert split == {"Food & Groceries": 2100, "Health": 125}

    async def test_negative_void_lines_survive(self):
        model = StubModel([
            save([{"description": "nuts", "amount": 11.74, "category": "Food & Groceries"},
                  {"description": "nuts void", "amount": -11.74, "category": "Food & Groceries"},
                  {"description": "rest", "amount": 22.25, "category": "Food & Groceries"}]),
            SUBMIT,
        ])
        r = await run_loop(model, image_b64="x", target_cents=2225, categories=CATS)
        assert any(l.cents < 0 for l in r.lines)
        assert r.closed is True
