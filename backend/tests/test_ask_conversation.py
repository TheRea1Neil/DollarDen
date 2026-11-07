"""The Ask agent has to remember the conversation it is in.

Without history a follow-up is meaningless -- "what about last month?"
has no referent -- so the chat looked conversational and was not.

History is bounded by TOKENS, not by a count of turns. A turn count
says nothing about how long each turn was, and the real limit is the
context window; raising CONTEXT_TOKENS on a bigger machine should buy
a longer conversation with no other change.
"""
from __future__ import annotations

import uuid
from datetime import date

import pytest

from app.ai import ask_agent
from app.db.models import User
from app.db.session import SessionLocal
from tests.conftest import requires_db

TODAY = date(2026, 11, 7)


class Recorder:
    """Captures the messages the agent builds, then answers in prose."""

    def __init__(self, prompt_tokens: int = 1234) -> None:
        self.seen: list[dict] = []
        self.prompt_tokens = prompt_tokens

    async def chat(self, _prompt, *, messages=None, tools=None, **kw):
        self.seen = list(messages or [])
        outer = self

        class R:
            content = "You spent $142.18."
            model = "gemma4:12b"
            tool_calls: list = []
            prompt_eval_count = outer.prompt_tokens
            eval_count = 10
            total_duration_ns = 1
            thinking = ""
            done_reason = "stop"

        return R()


def _user(db) -> User:
    u = User(email=f"ask-{uuid.uuid4().hex[:8]}@x.com", password_hash="x")
    db.add(u)
    db.flush()
    return u


@requires_db
class TestAskConversation:
    @pytest.mark.asyncio
    async def test_prior_turns_are_replayed_before_the_question(self) -> None:
        with SessionLocal() as db:
            u = _user(db)
            rec = Recorder()
            out = await ask_agent.run(
                db, u, rec, "what about last month?", today=TODAY,
                history=[{"question": "how much on fast food this month?",
                          "answer": "You spent $142.18."}],
            )

            roles = [m["role"] for m in rec.seen]
            assert roles[:2] == ["user", "assistant"], "earlier turn comes first"
            assert "fast food" in rec.seen[0]["content"]
            assert "$142.18" in rec.seen[1]["content"]
            assert "last month" in rec.seen[-1]["content"], "new question last"
            assert out.answer

    @pytest.mark.asyncio
    async def test_context_usage_is_measured_not_guessed(self) -> None:
        with SessionLocal() as db:
            u = _user(db)
            rec = Recorder(prompt_tokens=9001)
            out = await ask_agent.run(db, u, rec, "hello", today=TODAY,
                                      history=[])
            assert out.context_used == 9001, "ollama reports this; do not estimate"
            assert out.context_limit == ask_agent.CONTEXT_TOKENS
            assert out.as_dict()["context_used"] == 9001

    @pytest.mark.asyncio
    async def test_an_oversized_turn_is_dropped_and_reported(self) -> None:
        with SessionLocal() as db:
            u = _user(db)
            rec = Recorder()
            huge = "x" * (ask_agent.CONTEXT_TOKENS * 4)
            out = await ask_agent.run(
                db, u, rec, "and now?", today=TODAY,
                history=[{"question": "old", "answer": huge},
                         {"question": "recent", "answer": "short"}],
            )
            assert out.dropped_turns == 1
            kept = " ".join(str(m.get("content", "")) for m in rec.seen)
            assert "recent" in kept, "the newest turn is what a follow-up means"
            assert huge not in kept

    @pytest.mark.asyncio
    async def test_no_history_behaves_exactly_as_before(self) -> None:
        with SessionLocal() as db:
            u = _user(db)
            rec = Recorder()
            await ask_agent.run(db, u, rec, "how much on gas?", today=TODAY)
            assert len(rec.seen) == 1
            assert rec.seen[0]["role"] == "user"

    @pytest.mark.asyncio
    async def test_history_is_kept_newest_first_when_space_runs_out(self) -> None:
        """Dropping from the oldest end keeps what a follow-up refers to."""
        with SessionLocal() as db:
            u = _user(db)
            rec = Recorder()
            # Roughly 3,000 tokens a turn against a ~12,000 token budget,
            # so eight turns cannot all fit and the oldest must go.
            filler = "y" * 12000
            history = [{"question": f"q{i}", "answer": f"{filler} a{i}"}
                       for i in range(8)]
            out = await ask_agent.run(db, u, rec, "latest?", today=TODAY,
                                      history=history)
            kept = " ".join(str(m.get("content", "")) for m in rec.seen)
            assert out.dropped_turns > 0, "this should not all fit"
            assert "a7" in kept, "the most recent turn must survive"
            assert "a0" not in kept, "the oldest should be the one dropped"
