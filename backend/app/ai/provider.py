"""AI provider abstraction.

Three implementations behind one interface. NullProvider is a first-class
configuration: the app must be fully usable with AI disabled (ADR 0003).
"""
from __future__ import annotations

import abc
import json
import asyncio
import logging
import socket
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.config import Settings

log = logging.getLogger("nnf.ai")


class AIUnavailable(RuntimeError):
    """The provider could not be reached. Callers fall back to the review queue."""


@dataclass
class ChatResult:
    content: str
    model: str
    total_duration_ns: int | None = None
    eval_count: int | None = None
    # Tools the model asked us to run, each {"name": str, "arguments": dict}.
    # Empty for every non-tool call, so existing callers are unaffected.
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    # Reasoning text, when the model is a thinking one and thinking is on.
    # Kept separate from `content`: ollama returns them in different
    # fields, and a thinking model can fill this while leaving content
    # empty, which looks like a failure unless you can see it.
    thinking: str = ""
    # "stop" | "length" | ... -- `length` means the token budget ran out
    # mid-answer, which is a configuration problem, not a model one.
    done_reason: str | None = None

    @property
    def tokens_per_second(self) -> float | None:
        if not self.eval_count or not self.total_duration_ns:
            return None
        return self.eval_count / (self.total_duration_ns / 1e9)


class AIProvider(abc.ABC):
    @abc.abstractmethod
    async def chat(
        self,
        prompt: str,
        *,
        images: list[str] | None = None,
        schema: dict[str, Any] | None = None,
        model: str | None = None,
        temperature: float = 0.0,
        tools: list[dict[str, Any]] | None = None,
        messages: list[dict[str, Any]] | None = None,
        think: bool = False,
        num_ctx: int | None = None,
        num_predict: int | None = None,
    ) -> ChatResult: ...

    @abc.abstractmethod
    async def available(self) -> bool: ...


class NullProvider(AIProvider):
    """AI disabled. Everything routes to deterministic rules and the review queue."""

    async def chat(self, prompt: str, **_: Any) -> ChatResult:
        raise AIUnavailable(
            "AI features are switched off for this account. The rest of "
            "the budget works without them."
        )

    async def available(self) -> bool:
        return False


class OllamaProvider(AIProvider):
    """Talks to an Ollama server. Base for both the deck and user-supplied hosts."""

    def __init__(self, base_url: str, settings: Settings, timeout: float = 300.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.settings = settings
        self.timeout = timeout

    async def available(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                r = await c.get(f"{self.base_url}/api/version")
                return r.status_code == 200
        except Exception:
            return False

    async def chat(
        self,
        prompt: str,
        *,
        images: list[str] | None = None,
        schema: dict[str, Any] | None = None,
        model: str | None = None,
        temperature: float = 0.0,
        tools: list[dict[str, Any]] | None = None,
        messages: list[dict[str, Any]] | None = None,
        think: bool = False,
        num_ctx: int | None = None,
        num_predict: int | None = None,
    ) -> ChatResult:
        chosen = model or (
            self.settings.ai_model_vision if images else self.settings.ai_model_text
        )
        if messages is None:
            message: dict[str, Any] = {"role": "user", "content": prompt}
            if images:
                # Ollama takes base64-encoded images on the message.
                message["images"] = images
            messages = [message]

        payload: dict[str, Any] = {
            "model": chosen,
            "messages": messages,
            "stream": False,
            # Temperature 0 -- greedy decoding, no sampling.
            #
            # This was previously unset, which does NOT mean zero: ollama
            # falls back to each model's Modelfile, and those disagree
            # wildly. qwen2.5vl:7b ships temperature 0.0001; gemma3:12b
            # ships temperature 1 with top_k 64 and top_p 0.95. So the
            # same code was effectively deterministic on one model and
            # heavily sampled on another, with nothing in the request
            # saying so.
            #
            # Worse, it did not match what was measured. Every experiment
            # in experiments/ pins temperature 0 -- ADR 0006's allocation
            # arms and the categorisation precision figure both -- so the
            # numbers those ADRs quote described a configuration the app
            # never actually ran.
            #
            # Nothing here wants creativity. These calls extract figures,
            # pick a label from a fixed list, or compile a query. For all
            # of them the most likely token is the right one, and a
            # different answer to the same receipt on Tuesday is a bug.
            "options": {"temperature": temperature},
        }
        if tools:
            payload["tools"] = tools
        # Sent ALWAYS, including False.
        #
        # Omitting it does not mean "off" -- ollama falls back to the
        # model's own default, and gemma4 is a thinking model that
        # reasons by default. Left unsent it spent the whole token
        # budget thinking and returned empty content, which surfaces as
        #
        #     ValueError: model returned unparseable JSON
        #
        # intermittently, on any task, with nothing in the error
        # pointing at the cause. Same shape of bug as the temperature
        # one: unset is not a safe default, it is whatever the model
        # ships with.
        payload["think"] = bool(think)
        # A thinking model spends its token budget reasoning before it
        # answers. At ollama's default it ran out mid-thought and
        # returned an empty string -- which reads as a model failure and
        # is actually a budget one. Both are explicit here.
        if num_ctx is not None:
            payload["options"]["num_ctx"] = num_ctx
        if num_predict is not None:
            payload["options"]["num_predict"] = num_predict
        if schema is not None:
            # Constrain the output shape. More reliable than tool calling for
            # this workload, and vision-model tool support is less proven.
            payload["format"] = schema

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.post(f"{self.base_url}/api/chat", json=payload)
                r.raise_for_status()
                data = r.json()
        except Exception as exc:
            raise AIUnavailable(
                "The AI computer stopped responding part-way through. "
                "Nothing was saved — try again in a moment."
            ) from exc

        msg = data.get("message", {}) or {}
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:  # noqa: BLE001
                    args = {}
            calls.append({"name": fn.get("name"), "arguments": args or {}})

        return ChatResult(
            content=msg.get("content") or "",
            model=chosen,
            total_duration_ns=data.get("total_duration"),
            eval_count=data.get("eval_count"),
            tool_calls=calls,
            thinking=msg.get("thinking") or "",
            done_reason=data.get("done_reason"),
        )


class DeckProvider(OllamaProvider):
    """Ollama on the gaming PC, which is asleep by default.

    Wakes it via the already-deployed Wake-on-LAN API, then waits for the
    Ollama port to answer. Cold start is roughly 10s wake plus model load.
    """

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings.ai_deck_url, settings)

    @staticmethod
    def _port_open(host: str, port: int, timeout: float = 2.0) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    def _host_port(self) -> tuple[str, int]:
        hostport = self.base_url.split("//", 1)[-1]
        host, _, port = hostport.partition(":")
        return host, int(port or 11434)

    async def ensure_awake(self) -> None:
        host, port = self._host_port()
        if self._port_open(host, port):
            return

        s = self.settings
        if not s.wol_api_url or not s.wol_api_token:
            raise AIUnavailable(
                "The AI computer is asleep, and there is no way configured "
                "to wake it. Turn it on and try again."
            )

        log.info("AI host asleep; sending wake request")
        try:
            async with httpx.AsyncClient(timeout=s.ai_wake_timeout_seconds + 30) as c:
                r = await c.post(
                    f"{s.wol_api_url.rstrip('/')}/wake",
                    params={"wait": s.ai_wake_timeout_seconds},
                    headers={"Authorization": f"Bearer {s.wol_api_token}"},
                )
                r.raise_for_status()
        except Exception as exc:
            raise AIUnavailable(
                "Could not wake the AI computer. It may be unplugged or off "
                "the network — turn it on and try again."
            ) from exc

        # The host answers ICMP/SSH before Ollama is listening; poll the port.
        deadline = s.ai_wake_timeout_seconds
        waited = 0.0
        while waited < deadline:
            if self._port_open(host, port):
                log.info("AI host awake after ~%.0fs", waited)
                return
            await asyncio.sleep(2)
            waited += 2
        raise AIUnavailable(
            f"The AI computer did not finish starting up within {deadline} "
            f"seconds. It may still be booting — try again shortly."
        )

    async def chat(self, prompt: str, **kwargs: Any) -> ChatResult:
        await self.ensure_awake()
        return await super().chat(prompt, **kwargs)


def build_provider(settings: Settings, custom_url: str | None = None) -> AIProvider:
    if custom_url:
        return OllamaProvider(custom_url, settings)
    match settings.ai_provider:
        case "deck":
            return DeckProvider(settings)
        case "custom":
            return OllamaProvider(settings.ai_deck_url, settings)
        case _:
            return NullProvider()
