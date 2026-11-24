"""AI provider abstraction.

Three implementations behind one interface. NullProvider is a first-class
configuration: the app must be fully usable with AI disabled (ADR 0003).
"""
from __future__ import annotations

import abc
import json
import asyncio
import re
import logging
import socket
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.config import Settings

log = logging.getLogger("dollarden.ai")


class AIUnavailable(RuntimeError):
    """The provider could not be reached. Callers fall back to the review queue."""


class AIHostStalled(AIUnavailable):
    """The host accepted the request and then never answered.

    Worth its own type because it is the one failure no health check can
    see. Ollama's listener lives in the parent process; the model runs in
    a child `llama-server`. When that child wedges -- on 2026-11-16 it
    self-deadlocked inside the amdgpu KFD driver mapping the model into
    VRAM, state D, unkillable, see docs/16-deck-power.md -- the parent
    keeps answering the TCP connect AND /api/version, so every probe
    says "healthy" while generation hangs until the timeout.

    So this is detected by a request timing out, not by asking. It is
    separated from the asleep/offline cases because the remedy is
    different: those resolve themselves on the next wake, this one needs
    the machine power-cycled, and the queue should stop walking into it
    one job at a time.
    """


@dataclass
class ChatResult:
    content: str
    model: str
    total_duration_ns: int | None = None
    eval_count: int | None = None
    # Tokens the model READ: the whole conversation plus tool schemas.
    # This is what fills the context window, and ollama reports it, so
    # "how full is this chat" can be measured rather than guessed from
    # a turn count.
    prompt_eval_count: int | None = None
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


# Control tokens a model emits to structure its own output, which are
# meant for the runtime and not for a person.
#
# Observed in the wild: every Ask answer came back beginning
# "<channel|>You have spent...". The answer was right; the marker was
# simply never stripped, and went straight to the screen.
#
# Deliberately narrow. Only angle brackets containing a short run of
# letters, underscores, slashes or pipes are removed, so ordinary
# prose -- "<10 transactions", "a < b" -- is untouched. The pipe
# variants cover the <|im_start|> family as well as the bare form.
_CONTROL_TOKEN = re.compile(r"<\|?/?[a-z_]{2,20}\|?>", re.IGNORECASE)


def strip_control_tokens(text: str) -> str:
    """Remove a model's own structural markers from text shown to people."""
    if not text:
        return text
    return _CONTROL_TOKEN.sub("", text).strip()


class AIProvider(abc.ABC):
    @property
    def uses_local_hardware(self) -> bool:
        """Does running this consume a machine in the user's house?

        The job queue holds work when the graphics card is busy and
        overnight so the inference machine stays asleep. Neither means
        anything for a hosted model, and applying them anyway would
        silently defer categorisation until morning for someone whose
        AI is a web service. Answered here so the queue does not have
        to recognise provider types.
        """
        return True

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

    @property
    def uses_local_hardware(self) -> bool:
        return False

    async def chat(self, prompt: str, **_: Any) -> ChatResult:
        raise AIUnavailable(
            "AI features are switched off for this account. The rest of "
            "the budget works without them."
        )

    async def available(self) -> bool:
        return False


class OllamaProvider(AIProvider):
    """Talks to an Ollama server. Base for both the deck and user-supplied hosts."""

    def __init__(
        self, base_url: str, settings: Settings, timeout: float | None = None
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.settings = settings
        # Configurable because it is the only thing bounding a stalled
        # host: it was 300s hardcoded, so one wedged job cost 300s of
        # timeout on top of the wake wait, and the queue is serial.
        self.timeout = (
            timeout if timeout is not None else float(settings.ai_request_timeout_seconds)
        )

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
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # Never arrived. Checked before TimeoutException because
            # ConnectTimeout is one, and "could not be reached" and
            # "reached, then went quiet" need different advice.
            raise AIUnavailable(
                "The AI computer could not be reached. It may have gone to "
                "sleep or dropped off the network — try again in a moment."
            ) from exc
        except httpx.TimeoutException as exc:
            # Accepted the request, then nothing for the whole timeout.
            # A sleeping or busy host refuses or answers; silence this
            # long means the model process is stuck, and no amount of
            # waiting or retrying clears it.
            raise AIHostStalled(
                f"The AI computer is switched on but stopped answering "
                f"(nothing came back in {self.timeout:.0f} seconds). This "
                f"usually means it needs restarting. Nothing was saved."
            ) from exc
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
            # Stripped here rather than at each call site: it is an
            # artefact of the model, so every feature that shows model
            # text would otherwise need its own copy of this.
            content=strip_control_tokens(msg.get("content") or ""),
            model=chosen,
            total_duration_ns=data.get("total_duration"),
            eval_count=data.get("eval_count"),
            prompt_eval_count=data.get("prompt_eval_count"),
            tool_calls=calls,
            thinking=msg.get("thinking") or "",
            done_reason=data.get("done_reason"),
        )


class ManagedOllamaProvider(OllamaProvider):
    """An Ollama host this server is allowed to wake, and asleep by default.

    Wakes it via the already-deployed Wake-on-LAN API, then waits for the
    Ollama port to answer. Cold start is roughly 10s wake plus model load.
    """

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings.ai_host_url, settings)

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
        from app.ai import hardpower

        have_wol = bool(s.wol_api_url and s.wol_api_token)
        have_relay = hardpower.configured(s)
        if not have_wol and not have_relay:
            raise AIUnavailable(
                "The AI computer is asleep, and there is no way configured "
                "to wake it. Turn it on and try again."
            )

        deadline = s.ai_wake_timeout_seconds
        wol_failed = ""

        if have_wol:
            log.info("AI host asleep; sending wake request")
            try:
                async with httpx.AsyncClient(
                        timeout=deadline + 30) as c:
                    r = await c.post(
                        f"{s.wol_api_url.rstrip('/')}/wake",
                        params={"wait": deadline},
                        headers={"Authorization": f"Bearer {s.wol_api_token}"},
                    )
                    r.raise_for_status()
            except Exception as exc:  # noqa: BLE001
                # Do NOT give up here. A failing wake request is the
                # STRONGEST evidence the machine is fully off, which is
                # precisely when the power relay is the right answer --
                # and bailing out meant the relay was never reached at
                # all after a hard power-off.
                wol_failed = str(exc)
                log.info("wake request failed (%s); will try the relay", exc)
            else:
                # The host answers SSH before Ollama is listening, so
                # poll the port rather than trusting the wake call.
                waited = 0.0
                while waited < deadline:
                    if self._port_open(host, port):
                        log.info("AI host awake after ~%.0fs", waited)
                        return
                    await asyncio.sleep(2)
                    waited += 2

        # Wake-on-LAN did not get there -- either it errored or the
        # host never came up. WoL only reaches a NIC that still has
        # power, so a machine at soft off, after a power cut, or with
        # WoL disabled in the BIOS will never answer it however many
        # packets are sent. That is what the relay is for.
        if not have_relay:
            raise AIUnavailable(
                "Could not wake the AI computer. It may be unplugged or off "
                "the network — turn it on and try again."
                if wol_failed else
                f"The AI computer did not wake within {deadline} seconds. It "
                f"may be fully powered off — turn it on and try again."
            )

        result = await hardpower.press_once(s)
        if not result.pressed:
            raise AIUnavailable(
                f"The AI computer did not wake, and its power switch could "
                f"not be used: {result.detail}. Turn it on, then try again."
            )

        # Pressed once. A cold boot is POST plus a full SteamOS start,
        # so this waits far longer than a wake would -- and if it still
        # does not come up, it is reported rather than pressed again.
        # Repeatedly stabbing the power button of a machine that is
        # mid-boot is how a filesystem gets corrupted.
        boot_deadline = s.power_relay_boot_seconds
        waited = 0.0
        while waited < boot_deadline:
            if self._port_open(host, port):
                log.info("AI host booted after ~%.0fs via the power relay", waited)
                return
            await asyncio.sleep(3)
            waited += 3

        raise AIUnavailable(
            "The AI computer did not come up after its power button was "
            "pressed. It may still be booting, or it may need a look — "
            "give it a minute and try again. It will not be pressed again "
            "on its own."
        )

    async def chat(self, prompt: str, **kwargs: Any) -> ChatResult:
        await self.ensure_awake()
        return await super().chat(prompt, **kwargs)


class OpenAICompatibleProvider(AIProvider):
    """A hosted model, reached over an OpenAI-shaped API.

    One implementation reaches most of them, because most speak that
    shape: OpenAI, Gemini through its compatibility endpoint, anything
    on OpenRouter (Claude included), and local servers such as vLLM or
    LM Studio. Anthropic's own API is not OpenAI-shaped, so Claude
    direct would be a separate provider rather than a flag here.

    This app leans on four things Ollama does that have to be
    translated, and one of them is load bearing:

      * `format` -- a JSON schema the reply must satisfy. EVERY
        categorisation depends on it, and OpenAI calls it
        `response_format: json_schema`. Without it the model returns
        prose where the code expects fields.
      * `tools` -- already the same shape, so it passes through.
      * `num_predict` -- becomes `max_tokens`.
      * `think` and `num_ctx` -- meaningless on a hosted model and
        dropped rather than faked.

    Nothing here wakes anything or watches a graphics card: there is no
    local machine to manage, which is rather the point for people
    without one.
    """

    def __init__(self, settings: Settings, base_url: str | None = None,
                 api_key: str | None = None, timeout: float | None = None) -> None:
        self.settings = settings
        self.base_url = (base_url or settings.ai_api_base_url).rstrip("/")
        self.api_key = api_key or settings.ai_api_key
        self.timeout = (
            timeout if timeout is not None
            else float(settings.ai_request_timeout_seconds)
        )

    @property
    def uses_local_hardware(self) -> bool:
        return False

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def available(self) -> bool:
        if not (self.base_url and self.api_key):
            return False
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                r = await c.get(f"{self.base_url}/models", headers=self._headers())
                return r.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _with_images(content: str, images: list[str]) -> list[dict[str, Any]]:
        """OpenAI takes images as parts of the message, not beside it.

        Ollama accepts bare base64 in `images`; here each one becomes a
        `data:` URL in a content array. The media type is asserted as
        PNG because that is what this app produces -- receipts are
        re-encoded before they get here.
        """
        parts: list[dict[str, Any]] = [{"type": "text", "text": content}]
        for b64 in images:
            parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}"},
            })
        return parts

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
        if not (self.base_url and self.api_key):
            raise AIUnavailable(
                "No AI service is configured. Add the address and key in "
                "the server settings, or switch AI off."
            )

        chosen = model or (
            self.settings.ai_model_vision if images
            else self.settings.ai_model_text
        )
        if messages is None:
            content: Any = prompt
            if images:
                content = self._with_images(prompt, images)
            messages = [{"role": "user", "content": content}]

        payload: dict[str, Any] = {
            "model": chosen,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
        if num_predict is not None:
            payload["max_tokens"] = num_predict
        if schema is not None:
            # The load-bearing translation. `strict` makes the service
            # enforce the shape rather than merely encourage it, which
            # is the whole reason categorisation can trust the reply.
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "reply", "schema": schema, "strict": True},
            }
        # `think` and `num_ctx` are deliberately not sent. A hosted
        # model has no equivalent, and inventing one would be a lie
        # that only shows up as a confusing error much later.

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.post(f"{self.base_url}/chat/completions",
                                 json=payload, headers=self._headers())
                if r.status_code == 401:
                    raise AIUnavailable(
                        "The AI service rejected the key. Check it in the "
                        "server settings."
                    )
                if r.status_code == 429:
                    raise AIUnavailable(
                        "The AI service is rate limiting this account. "
                        "Try again shortly."
                    )
                r.raise_for_status()
                data = r.json()
        except AIUnavailable:
            raise
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise AIUnavailable(
                "The AI service could not be reached — check the address "
                "and your connection, then try again."
            ) from exc
        except httpx.TimeoutException as exc:
            # Unlike a wedged machine of your own, a service that
            # went quiet is usually a passing thing -- so "try again"
            # is the right advice here, where "restart it" would be
            # advice about a computer the user does not have.
            raise AIHostStalled(
                f"The AI service took the request and then stopped "
                f"answering (nothing came back in {self.timeout:.0f} "
                f"seconds). Nothing was saved — try again in a moment."
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise AIUnavailable(
                "The AI service stopped responding part-way through. "
                "Nothing was saved — try again in a moment."
            ) from exc

        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}

        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, str):
                # OpenAI sends arguments as a JSON STRING; Ollama sends
                # an object. Callers get an object either way.
                try:
                    args = json.loads(args)
                except Exception:  # noqa: BLE001
                    args = {}
            calls.append({"name": fn.get("name"), "arguments": args or {}})

        usage = data.get("usage") or {}
        return ChatResult(
            content=strip_control_tokens(msg.get("content") or ""),
            model=data.get("model") or chosen,
            eval_count=usage.get("completion_tokens"),
            # Same meaning as Ollama's prompt_eval_count, so the Ask
            # screen's context meter keeps working unchanged.
            prompt_eval_count=usage.get("prompt_tokens"),
            tool_calls=calls,
            thinking="",
            done_reason=choice.get("finish_reason"),
        )


def build_provider(settings: Settings, custom_url: str | None = None) -> AIProvider:
    if custom_url:
        return OllamaProvider(custom_url, settings)
    match settings.ai_provider:
        case "openai":
            return OpenAICompatibleProvider(settings)
        case "managed":
            return ManagedOllamaProvider(settings)
        case "custom":
            # Expected to be there already; nothing to wake.
            return OllamaProvider(settings.ai_host_url, settings)
        case _:
            return NullProvider()
