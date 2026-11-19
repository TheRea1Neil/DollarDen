"""A hosted model, for people without a spare machine.

Self-hosting is what this project is for, and it needs a GPU box that
most people do not have. Refusing to support anything else does not
make them self-host; it makes them use something else, or run a budget
with no categorisation at all.

One implementation reaches most services, because most speak OpenAI's
shape: OpenAI, Gemini through its compatibility endpoint, anything on
OpenRouter including Claude, and local servers like vLLM. What has to
be right is the translation — particularly `format` to
`response_format`, which every categorisation in this app depends on.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest

from app.ai.provider import (AIHostStalled, AIUnavailable, NullProvider,
                             OpenAICompatibleProvider, build_provider)
from app.core.config import Settings


def _settings(**over) -> Settings:
    base = dict(allow_production=False, ai_provider="openai",
                ai_api_base_url="https://api.example.com/v1",
                ai_api_key="sk-test", ai_model_text="some-model")
    base.update(over)
    return Settings(**base)


class _Capture:
    """Stands in for the service, and remembers what it was sent."""

    def __init__(self, reply: dict | None = None, status: int = 200,
                 raises: Exception | None = None) -> None:
        self.sent: dict = {}
        self.headers: dict = {}
        self.reply = reply if reply is not None else {
            "model": "some-model",
            "choices": [{"message": {"content": "hello"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 3},
        }
        self.status = status
        self.raises = raises

    def __call__(self, **kw):
        outer = self

        class _Resp:
            status_code = outer.status

            def raise_for_status(self):
                if outer.status >= 400:
                    raise httpx.HTTPStatusError("boom", request=None, response=None)

            def json(self):
                return outer.reply

        class _Client:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False

            async def post(self, url, json=None, headers=None):
                if outer.raises:
                    raise outer.raises
                outer.sent = json or {}
                outer.headers = headers or {}
                outer.url = url
                return _Resp()

            async def get(self, url, headers=None):
                return _Resp()

        return _Client()


async def _chat(cap: _Capture, **kw):
    p = OpenAICompatibleProvider(_settings())
    with patch("httpx.AsyncClient", cap):
        return await p.chat(kw.pop("prompt", "hi"), **kw)


class TestItIsSelected:
    def test_the_setting_builds_it(self) -> None:
        assert isinstance(build_provider(_settings()), OpenAICompatibleProvider)

    def test_none_still_wins(self) -> None:
        assert isinstance(build_provider(_settings(ai_provider="none")),
                          NullProvider)


class TestTheTranslation:
    @pytest.mark.asyncio
    async def test_a_schema_becomes_response_format(self) -> None:
        """The load-bearing one: every categorisation depends on it."""
        cap = _Capture()
        schema = {"type": "object", "properties": {"category": {"type": "string"}}}
        await _chat(cap, schema=schema)

        rf = cap.sent["response_format"]
        assert rf["type"] == "json_schema"
        assert rf["json_schema"]["schema"] == schema
        assert rf["json_schema"]["strict"] is True, \
            "enforced, not merely encouraged"
        assert "format" not in cap.sent, "that is Ollama's spelling"

    @pytest.mark.asyncio
    async def test_num_predict_becomes_max_tokens(self) -> None:
        cap = _Capture()
        await _chat(cap, num_predict=256)
        assert cap.sent["max_tokens"] == 256
        assert "num_predict" not in cap.sent

    @pytest.mark.asyncio
    async def test_ollama_only_options_are_not_invented(self) -> None:
        """A hosted model has no equivalent; faking one fails later."""
        cap = _Capture()
        await _chat(cap, think=True, num_ctx=16384)
        assert "think" not in cap.sent
        assert "num_ctx" not in cap.sent
        assert "options" not in cap.sent

    @pytest.mark.asyncio
    async def test_tools_pass_straight_through(self) -> None:
        cap = _Capture()
        tools = [{"type": "function", "function": {"name": "payday",
                                                   "parameters": {}}}]
        await _chat(cap, tools=tools)
        assert cap.sent["tools"] == tools

    @pytest.mark.asyncio
    async def test_images_become_message_parts(self) -> None:
        """Ollama takes bare base64 beside the text; OpenAI does not."""
        cap = _Capture()
        await _chat(cap, prompt="what is this", images=["QUJD"])
        content = cap.sent["messages"][0]["content"]
        assert isinstance(content, list)
        assert content[0] == {"type": "text", "text": "what is this"}
        assert content[1]["image_url"]["url"].startswith("data:image/png;base64,QUJD")

    @pytest.mark.asyncio
    async def test_the_key_is_sent_as_a_bearer_token(self) -> None:
        cap = _Capture()
        await _chat(cap)
        assert cap.headers["Authorization"] == "Bearer sk-test"


class TestTheReply:
    @pytest.mark.asyncio
    async def test_content_and_token_counts_come_back(self) -> None:
        """prompt_tokens is the same meaning as Ollama's
        prompt_eval_count, so the Ask screen's context meter survives."""
        cap = _Capture()
        out = await _chat(cap)
        assert out.content == "hello"
        assert out.prompt_eval_count == 11
        assert out.eval_count == 3
        assert out.done_reason == "stop"

    @pytest.mark.asyncio
    async def test_tool_arguments_are_parsed_from_their_json_string(self) -> None:
        """OpenAI sends arguments as a STRING; Ollama sends an object.

        Callers must not have to know which service answered.
        """
        cap = _Capture(reply={
            "choices": [{"message": {"content": "", "tool_calls": [
                {"function": {"name": "spending",
                              "arguments": json.dumps({"period": "this_month"})}}
            ]}, "finish_reason": "tool_calls"}],
            "usage": {},
        })
        out = await _chat(cap)
        assert out.tool_calls == [
            {"name": "spending", "arguments": {"period": "this_month"}}]

    @pytest.mark.asyncio
    async def test_control_tokens_are_stripped_here_too(self) -> None:
        cap = _Capture(reply={
            "choices": [{"message": {"content": "<channel|>the answer"},
                         "finish_reason": "stop"}],
            "usage": {},
        })
        assert (await _chat(cap)).content == "the answer"


class TestFailuresSayWhatToDo:
    @pytest.mark.asyncio
    async def test_a_rejected_key_says_so(self) -> None:
        cap = _Capture(status=401)
        with pytest.raises(AIUnavailable, match="rejected the key"):
            await _chat(cap)

    @pytest.mark.asyncio
    async def test_rate_limiting_is_not_reported_as_a_broken_key(self) -> None:
        cap = _Capture(status=429)
        with pytest.raises(AIUnavailable, match="rate limiting"):
            await _chat(cap)

    @pytest.mark.asyncio
    async def test_a_timeout_is_its_own_failure(self) -> None:
        cap = _Capture(raises=httpx.ReadTimeout("slow"))
        with pytest.raises(AIHostStalled):
            await _chat(cap)

    @pytest.mark.asyncio
    async def test_unconfigured_says_what_is_missing(self) -> None:
        p = OpenAICompatibleProvider(_settings(ai_api_key="", ai_api_base_url=""))
        with pytest.raises(AIUnavailable, match="No AI service is configured"):
            await p.chat("hi")


class TestItDoesNotPretendToOwnHardware:
    """Quiet hours and the GPU gate exist for a machine in the house."""

    def test_a_hosted_model_claims_no_local_hardware(self) -> None:
        assert OpenAICompatibleProvider(_settings()).uses_local_hardware is False

    def test_ollama_does(self) -> None:
        from app.ai.provider import ManagedOllamaProvider

        s = Settings(allow_production=False, ai_provider="managed")
        assert ManagedOllamaProvider(s).uses_local_hardware is True

    def test_the_queue_asks_rather_than_assuming(self) -> None:
        import inspect

        from app.ai import jobs

        src = inspect.getsource(jobs.run_job)
        assert "provider.uses_local_hardware" in src, (
            "otherwise a hosted model is deferred until morning")
