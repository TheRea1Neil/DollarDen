"""The MCP endpoints: what they expose, and to whom.

This is the first interface where something other than a person drives
the system, so the boundaries matter more than the conveniences. Two
endpoints rather than one role-gated endpoint, because the separation
is the point: someone can hand a general assistant the budget tools
and know it cannot touch the server's Plaid credentials whatever it
decides to try.

The other rule being enforced here is that tools call the same
functions the app calls. A tool that computed its own totals would
drift from the screens, and that drift surfaces as an assistant
confidently quoting a figure the app disagrees with.
"""
from __future__ import annotations

import uuid

import pytest

from app.core.security import hash_password
from app.db.models import User
from app.db.session import SessionLocal
from tests.conftest import requires_db


def _make(role: str = "member") -> tuple[str, str]:
    email = f"mcp-{uuid.uuid4().hex[:8]}@example.com"
    pw = "a-long-enough-password"
    with SessionLocal() as db:
        db.add(User(email=email, password_hash=hash_password(pw),
                    role=role, timezone="UTC"))
        db.commit()
    return email, pw


def _auth(client, role: str = "member") -> dict[str, str]:
    email, pw = _make(role)
    r = client.post("/api/auth/login", json={"email": email, "password": pw})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _rpc(client, path: str, method: str, headers, params=None, mid=1):
    body = {"jsonrpc": "2.0", "id": mid, "method": method}
    if params is not None:
        body["params"] = params
    return client.post(path, json=body, headers=headers)


@requires_db
class TestTheTwoSurfacesAreSeparate:
    def test_a_member_may_use_the_budget_tools(self, client) -> None:
        r = _rpc(client, "/api/mcp/budget", "tools/list", _auth(client, "member"))
        assert r.status_code == 200
        names = {t["name"] for t in r.json()["result"]["tools"]}
        assert "budget_envelopes" in names

    def test_a_member_is_refused_the_admin_tools(self, client) -> None:
        """The whole reason for two endpoints."""
        r = _rpc(client, "/api/mcp/admin", "tools/list", _auth(client, "member"))
        assert r.status_code == 403

    def test_a_stranger_is_refused_both(self, client) -> None:
        for path in ("/api/mcp/budget", "/api/mcp/admin"):
            r = _rpc(client, path, "tools/list", {})
            assert r.status_code in (401, 403), path

    def test_admin_tools_are_not_reachable_from_the_budget_endpoint(
            self, client) -> None:
        """An owner on the budget endpoint still gets only budget tools.

        Otherwise handing over "just the budget" would quietly hand
        over the server as well, for the one account that matters most.
        """
        r = _rpc(client, "/api/mcp/budget", "tools/list", _auth(client, "owner"))
        names = {t["name"] for t in r.json()["result"]["tools"]}
        assert not any(n.startswith("admin_") for n in names)

    def test_calling_an_admin_tool_by_name_from_budget_fails(self, client) -> None:
        r = _rpc(client, "/api/mcp/budget", "tools/call", _auth(client, "owner"),
                 {"name": "admin_change_settings",
                  "arguments": {"values": {"ai_provider": "none"}}})
        assert r.json()["result"]["isError"] is True


@requires_db
class TestTheProtocol:
    def test_initialize_declares_tools(self, client) -> None:
        r = _rpc(client, "/api/mcp/budget", "initialize", _auth(client),
                 {"protocolVersion": "2024-11-05", "capabilities": {}})
        result = r.json()["result"]
        assert "tools" in result["capabilities"]
        assert result["serverInfo"]["name"]

    def test_a_notification_gets_no_reply(self, client) -> None:
        """Answering one leaves the client waiting for a response that
        the spec says will never come."""
        r = client.post("/api/mcp/budget",
                        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                        headers=_auth(client))
        assert r.status_code == 202
        assert r.content in (b"", b"null")

    def test_an_unknown_method_is_a_proper_jsonrpc_error(self, client) -> None:
        r = _rpc(client, "/api/mcp/budget", "resources/list", _auth(client))
        assert r.json()["error"]["code"] == -32601

    def test_a_non_json_body_does_not_crash(self, client) -> None:
        r = client.post("/api/mcp/budget", content=b"not json",
                        headers={**_auth(client), "Content-Type": "application/json"})
        assert r.status_code == 400
        assert r.json()["error"]["code"] == -32700

    def test_every_tool_describes_itself(self, client) -> None:
        """A tool a model cannot understand is a tool it will misuse."""
        r = _rpc(client, "/api/mcp/budget", "tools/list", _auth(client))
        for t in r.json()["result"]["tools"]:
            assert len(t["description"]) > 40, t["name"]
            assert t["inputSchema"]["type"] == "object"


@requires_db
class TestToolErrorsAreRecoverable:
    """A model must be able to read what went wrong and try again.

    Raising through the transport would end the conversation; these
    come back as tool results flagged as errors instead.
    """

    def test_an_unknown_tool_lists_the_real_ones(self, client) -> None:
        r = _rpc(client, "/api/mcp/budget", "tools/call", _auth(client),
                 {"name": "budget_nonsense", "arguments": {}})
        result = r.json()["result"]
        assert result["isError"] is True
        assert "budget_envelopes" in result["content"][0]["text"]

    def test_a_bad_period_suggests_valid_ones(self, client) -> None:
        r = _rpc(client, "/api/mcp/budget", "tools/call", _auth(client),
                 {"name": "budget_spending", "arguments": {"period": "last tuesday"}})
        result = r.json()["result"]
        assert result["isError"] is True
        assert "this_month" in result["content"][0]["text"]

    def test_wrong_arguments_say_which(self, client) -> None:
        r = _rpc(client, "/api/mcp/budget", "tools/call", _auth(client),
                 {"name": "budget_calculate", "arguments": {"wrong": 1}})
        assert r.json()["result"]["isError"] is True


@requires_db
class TestToolsReuseTheRealCode:
    def test_calculate_is_the_same_one_the_assistant_uses(self, client) -> None:
        r = _rpc(client, "/api/mcp/budget", "tools/call", _auth(client),
                 {"name": "budget_calculate", "arguments": {"expression": "162.56 / 3"}})
        text = r.json()["result"]["content"][0]["text"]
        assert "54.19" in text, "same rounding as the app"

    def test_the_calculator_still_refuses_what_it_always_refused(
            self, client) -> None:
        """Reached through a new door, with the same lock on it."""
        r = _rpc(client, "/api/mcp/budget", "tools/call", _auth(client),
                 {"name": "budget_calculate",
                  "arguments": {"expression": "__import__('os').system('ls')"}})
        assert r.json()["result"]["isError"] is True

    def test_every_read_tool_actually_runs(self, client) -> None:
        """The gap that let a wrong attribute name ship.

        `budget_spending` was only ever exercised down its error path --
        a bad period -- so nobody noticed it read `previous_cents` from
        a result that calls the field `previous_total_cents`. It threw
        the first time it was pointed at a real budget. Every read tool
        is now called for real, with its default arguments, on an
        account that has no data: an empty budget is a perfectly good
        test of whether the code runs at all.
        """
        headers = _auth(client, "owner")
        listed = _rpc(client, "/api/mcp/budget", "tools/list", headers)
        names = [t["name"] for t in listed.json()["result"]["tools"]]
        assert names, "no tools to check"

        for name in names:
            args = {"expression": "1 + 1"} if name == "budget_calculate" else {}
            r = _rpc(client, "/api/mcp/budget", "tools/call", headers,
                     {"name": name, "arguments": args})
            result = r.json()["result"]
            assert not result.get("isError"), (
                f"{name} failed: {result['content'][0]['text'][:200]}")

    def test_spending_returns_the_fields_it_promises(self, client) -> None:
        import json as _json

        r = _rpc(client, "/api/mcp/budget", "tools/call", _auth(client),
                 {"name": "budget_spending", "arguments": {"period": "this_month"}})
        body = _json.loads(r.json()["result"]["content"][0]["text"])
        for field in ("period", "total", "total_cents", "transactions"):
            assert field in body, field

    def test_envelopes_come_from_the_budget_not_a_copy(self, client) -> None:
        import inspect

        from app.mcp import budget_tools

        src = inspect.getsource(budget_tools)
        assert "Q.run(" in src, "spending must use the analytics queries"
        assert "_run_payday" in src, "payday must use the assistant's own tool"


class TestAdminToolsDoNotMintCredentials:
    def test_there_is_no_account_or_password_tool(self) -> None:
        """Both exist in the API. Neither belongs to an assistant.

        Helping someone through setup does not require being able to
        create logins on a server holding their bank connections.
        """
        import inspect

        from app.mcp import admin_tools

        src = inspect.getsource(admin_tools)
        for forbidden in ("create_user", "set_password", "hash_password"):
            assert forbidden not in src, forbidden
