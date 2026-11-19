"""Model control tokens must not reach the screen.

Found on the real instance: every Ask answer came back beginning
`<channel|>`. The figures were right and the sentence was right; the
model's own structural marker was simply never stripped and went
straight to the app.

Stripped at the provider because it is an artefact of the model, not
of any one feature. Otherwise Ask, the categorisation reasons, the
summaries and everything added later each need their own copy of this,
and the one that forgets is the one that ships.

The pattern is deliberately narrow. A budget is full of text that
looks a little like a tag -- "<10 transactions", "a < b", a merchant
named in angle brackets -- and eating any of that would be a worse bug
than the one being fixed.
"""
from __future__ import annotations

import pytest

from app.ai.provider import strip_control_tokens


class TestTokensAreRemoved:
    @pytest.mark.parametrize("raw,clean", [
        ("<channel|>You have spent $223.24 on food.",
         "You have spent $223.24 on food."),
        ("<|channel|>answer", "answer"),
        ("<|im_start|>assistant text", "assistant text"),
        ("<|im_end|>", ""),
        ("<think>reasoning</think>done", "reasoningdone"),
    ])
    def test_the_marker_goes(self, raw, clean) -> None:
        assert strip_control_tokens(raw) == clean

    def test_the_real_one_that_was_observed(self) -> None:
        """Verbatim from the instance, before the fix."""
        raw = ("<channel|>You have spent a total of **$223.24** on food this "
               "month (categorized under **Food And Groceries**).")
        out = strip_control_tokens(raw)
        assert out.startswith("You have spent")
        assert "$223.24" in out, "the figure survives"
        assert "**Food And Groceries**" in out, "and so does the markdown"


class TestOrdinaryTextIsLeftAlone:
    """The failure mode of an eager fix: eating real content."""

    @pytest.mark.parametrize("text", [
        "You had <10 transactions this week",
        "a < b and c > d",
        "Spent $5 at <Unknown Merchant>",
        "Rent is < half your income",
        "Use the <- arrow",
        "",
    ])
    def test_it_survives_untouched(self, text) -> None:
        assert strip_control_tokens(text) == text.strip()

    def test_a_long_bracketed_phrase_is_not_a_token(self) -> None:
        """Control tokens are short; sentences are not."""
        text = "<this is clearly not a control token>"
        assert strip_control_tokens(text) == text


class TestItHappensAtTheProvider:
    def test_chat_results_are_cleaned(self) -> None:
        """So every feature benefits, not just the one that noticed."""
        import inspect

        from app.ai import provider as mod

        src = inspect.getsource(mod.OllamaProvider.chat)
        assert "strip_control_tokens(" in src
