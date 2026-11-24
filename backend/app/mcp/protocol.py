"""The MCP wire protocol, just enough of it for a server of tools.

MCP is JSON-RPC 2.0 over HTTP. A server that offers tools and nothing
else needs four methods, which is why this is written out rather than
taken as a dependency: the surface is small, it is testable here, and
it does not move underneath a self-hosted install that someone
upgrades twice a year.

    initialize                 handshake, declares what the server does
    notifications/initialized  client says it is ready; no reply
    tools/list                 what can be called
    tools/call                 call one

Everything else returns a proper JSON-RPC "method not found" rather
than a 500, so a client probing for prompts or resources gets a clear
answer instead of a crash.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

log = logging.getLogger("dollarden.mcp")

PROTOCOL_VERSION = "2024-11-05"

# JSON-RPC error codes. The last is ours, within the reserved range
# for implementation-defined server errors.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


@dataclass
class Tool:
    """One callable thing, and how to describe it to a model.

    `handler` receives the validated arguments and returns anything
    JSON-serialisable. Raising `ValueError` produces a tool error the
    model can read and correct; anything else is logged and reported
    without leaking internals.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., Awaitable[Any] | Any]

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


def _result(request_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": code, "message": message}}


def _text(value: Any) -> dict[str, Any]:
    """Tool output, as MCP wants it.

    Everything is returned as text, including structured results,
    because that is what the content blocks carry. JSON is formatted
    rather than minified: a model reads this, and so does whoever is
    debugging why it answered the way it did.
    """
    if isinstance(value, str):
        body = value
    else:
        body = json.dumps(value, indent=2, default=str)
    return {"content": [{"type": "text", "text": body}]}


async def handle(message: dict[str, Any], tools: dict[str, Tool], *,
                 server_name: str, version: str) -> dict[str, Any] | None:
    """Answer one JSON-RPC message. None means "notification, say nothing"."""
    if message.get("jsonrpc") != "2.0":
        return _error(message.get("id"), INVALID_REQUEST,
                      "Expected a JSON-RPC 2.0 message")

    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    # Notifications carry no id and must not be answered at all.
    if method == "notifications/initialized" or request_id is None:
        return None

    if method == "initialize":
        return _result(request_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": server_name, "version": version},
        })

    if method == "tools/list":
        return _result(request_id, {"tools": [t.describe() for t in tools.values()]})

    if method == "tools/call":
        name = params.get("name")
        tool = tools.get(name)
        if tool is None:
            # A tool error rather than a protocol error: the model
            # asked for something reasonable and got it wrong, and it
            # can recover from being told so.
            return _result(request_id, {
                **_text(f"No tool called {name!r}. "
                        f"Available: {', '.join(sorted(tools))}"),
                "isError": True,
            })
        args = params.get("arguments") or {}
        try:
            out = tool.handler(**args)
            if hasattr(out, "__await__"):
                out = await out
            return _result(request_id, _text(out))
        except TypeError as exc:
            return _result(request_id, {
                **_text(f"Wrong arguments for {name}: {exc}"), "isError": True})
        except ValueError as exc:
            return _result(request_id, {**_text(str(exc)), "isError": True})
        except Exception:  # noqa: BLE001
            log.exception("mcp tool %s failed", name)
            return _result(request_id, {
                **_text(f"{name} failed. The server logged the details."),
                "isError": True})

    return _error(request_id, METHOD_NOT_FOUND, f"Unknown method {method!r}")
