"""The `calculate` tool: arithmetic the model asks for but never does.

Every figure in an answer has to be right, and "162.56 divided by 3" is
exactly the kind of thing a language model produces confidently and
occasionally wrongly. So it decides WHICH sum answers the question and
the engine computes it — ADR 0006 and 0009, applied to the arithmetic
on top of the data rather than to the data itself.

The expression comes from the model, which makes this the one place in
the app that evaluates a string written by one. `eval` would hand it
the process, so the expression is parsed and walked instead: numbers,
arithmetic operators, and four named functions. The security tests
below are not hypothetical — they are the reason it is written this
way.
"""
from __future__ import annotations

from app.ai.ask_agent import _run_calculate, tools_for


def _calc(expr: str):
    return _run_calculate({"expression": expr})


class TestItGetsTheSumRight:
    def test_plain_division(self):
        out = _calc("162.56 / 3")
        assert out["result"] == 54.1867
        assert out["as_money"] == "$54.19"

    def test_precedence_and_brackets(self):
        assert _calc("(220 - 45) / 7")["result"] == 25.0
        assert _calc("2 + 3 * 4")["result"] == 14

    def test_the_named_helpers(self):
        assert _calc("round(54.1866, 2)")["result"] == 54.19
        assert _calc("abs(0 - 12)")["result"] == 12
        assert _calc("min(40, 55)")["result"] == 40
        assert _calc("max(40, 55)")["result"] == 55

    def test_it_echoes_what_it_computed(self):
        """The step is shown in the UI; it has to say what was asked."""
        assert _calc("10 / 4")["expression"] == "10 / 4"

    def test_a_long_decimal_is_not_handed_back_raw(self):
        """162.56/3 is 54.18666666666667, which belongs in no answer."""
        assert len(str(_calc("162.56 / 3")["result"]).split(".")[-1]) <= 4


class TestItFailsAsATooolErrorNotAnException:
    """A bad expression must come back as something the model can read.

    An exception here kills the whole job and the question goes
    unanswered; an error string lets it correct itself and carry on.
    """

    def test_divide_by_zero(self):
        assert "zero" in _calc("5 / 0")["error"]
        assert "zero" in _calc("5 // 0")["error"]
        assert "zero" in _calc("5 % 0")["error"]

    def test_nonsense_is_refused(self):
        assert "error" in _calc("not an expression at all")
        assert "error" in _calc("")
        assert "error" in _calc("((((")

    def test_absurd_powers_do_not_hang(self):
        """9**9**9 parses fine and would never return."""
        assert "error" in _calc("9 ** 9 ** 9")
        assert "error" in _calc("99999999 ** 99999999")

    def test_an_overlong_expression_is_refused(self):
        assert "error" in _calc("1 + " * 100 + "1")


class TestItCannotBeTalkedIntoAnythingElse:
    """The expression is written by a language model. Treat it as such."""

    def test_no_imports_or_builtins(self):
        for attempt in [
            "__import__('os').system('ls')",
            "open('/etc/passwd').read()",
            "exec('x=1')",
            "eval('1+1')",
        ]:
            assert "error" in _calc(attempt), f"allowed: {attempt}"

    def test_no_attribute_access(self):
        for attempt in ["(1).__class__", "().__class__.__bases__"]:
            assert "error" in _calc(attempt), f"allowed: {attempt}"

    def test_no_bare_names(self):
        """No variables exist, and a name is how you reach a builtin."""
        assert "error" in _calc("x + 1")
        assert "error" in _calc("globals")

    def test_no_strings_or_lists(self):
        """'a' * 10000000 is a memory problem wearing arithmetic's clothes."""
        assert "error" in _calc("'a' * 1000")
        assert "error" in _calc("[1] * 1000")

    def test_booleans_are_not_numbers(self):
        assert "error" in _calc("True + 1")

    def test_unapproved_functions_are_refused(self):
        assert "error" in _calc("pow(2, 3)")
        assert "error" in _calc("sum([1, 2])")


class TestTheToolIsOffered:
    def test_calculate_is_in_the_tool_list(self):
        names = {t["function"]["name"] for t in tools_for([], [])}
        assert "calculate" in names

    def test_it_takes_an_expression(self):
        tool = next(t["function"] for t in tools_for([], [])
                    if t["function"]["name"] == "calculate")
        assert "expression" in tool["parameters"]["properties"]
        assert tool["parameters"]["required"] == ["expression"]

    def test_the_prompt_sends_every_sum_here(self):
        from app.ai.ask_agent import PROMPT

        assert "calculate" in PROMPT
        assert "never do it in your head" in PROMPT
