"""The developer docs must describe what the code actually does.

These exist so someone building a wake service — quite likely with an
assistant helping — can fetch the exact contract instead of inferring
it. That only works if the contract is true.

A document that drifts from the implementation is worse than no
document at all: it sends someone to build the wrong thing, and then
their wiring gets the blame. So the tests below compare the published
spec against the code that does the calling, rather than checking the
prose reads nicely.
"""
from __future__ import annotations

import inspect

import pytest

from app import devdocs


class TestTheIndex:
    def test_it_lists_the_topics(self) -> None:
        topics = {t["topic"] for t in devdocs.index()["topics"]}
        assert topics == {"wake-contract", "gpu-probe"}

    def test_every_topic_says_what_it_is_for(self) -> None:
        for t in devdocs.index()["topics"]:
            assert len(t["summary"]) > 60, t["topic"]

    def test_lookup_is_forgiving_about_case(self) -> None:
        assert devdocs.get("Wake-Contract") is not None
        assert devdocs.get("nonsense") is None


class TestTheWakeContractMatchesTheCode:
    """Compared against `ai/hardpower.py`, which does the calling."""

    @pytest.fixture
    def src(self) -> str:
        from app.ai import hardpower

        return inspect.getsource(hardpower)

    def test_the_documented_paths_are_the_ones_called(self, src) -> None:
        paths = [e["path"] for e in devdocs.WAKE_CONTRACT["endpoints"]]
        assert any(p.endswith("/press") for p in paths)
        assert any(p.endswith("/status") for p in paths)
        assert '{url}/press' in src
        assert '{url}/status' in src

    def test_the_documented_status_codes_are_the_ones_handled(self, src) -> None:
        """409 and 429 are not decoration — the caller branches on them."""
        documented = set(devdocs.WAKE_CONTRACT["endpoints"][0]["responses"])
        assert {"200", "409", "429"} <= documented
        for code in ("200", "409", "429"):
            assert f"status_code == {code}" in src, code

    def test_the_retry_field_is_named_the_same(self, src) -> None:
        """The caller reads this key by name; a doc that renames it
        sends someone to return the wrong thing."""
        body = devdocs.WAKE_CONTRACT["endpoints"][0]["responses"]["429"]["body"]
        assert "retry_after_seconds" in body
        assert "retry_after_seconds" in src

    def test_the_settings_named_are_real(self) -> None:
        from app.core.config import Settings

        fields = set(Settings.model_fields)
        for s in devdocs.WAKE_CONTRACT["settings"]:
            assert s["name"].lower() in fields, s["name"]

    def test_the_auth_header_matches(self, src) -> None:
        headers = devdocs.WAKE_CONTRACT["endpoints"][0]["headers"]
        assert headers["Authorization"].startswith("Bearer ")
        assert 'f"Bearer {settings.power_relay_token}"' in src


class TestTheGpuProbeMatchesTheCode:
    def test_the_settings_named_are_real(self) -> None:
        from app.core.config import Settings

        fields = set(Settings.model_fields)
        for s in devdocs.GPU_PROBE["settings"]:
            assert s["name"].lower() in fields, s["name"]

    def test_the_documented_fields_are_the_ones_read(self) -> None:
        from app.ai import gpu

        src = inspect.getsource(gpu)
        body = devdocs.GPU_PROBE["endpoints"][0]["responses"]["200"]["body"]
        for key in ("busy_percent", "vram_free_mb"):
            assert key in body
            assert key in src, key


class TestTheDangerousRulesAreStated:
    """The two that cost real damage if someone does not know them."""

    def test_it_warns_against_pressing_a_running_machine(self) -> None:
        text = " ".join(devdocs.WAKE_CONTRACT["rules"]).lower()
        assert "already up" in text
        assert "shutdown" in text, "say WHY, or it reads as fussiness"

    def test_it_warns_about_pressing_during_a_boot(self) -> None:
        text = " ".join(devdocs.WAKE_CONTRACT["rules"]).lower()
        assert "cooldown" in text
        assert "corrupt" in text

    def test_it_says_not_to_expose_a_power_switch(self) -> None:
        text = " ".join(devdocs.WAKE_CONTRACT["rules"]).lower()
        assert "authentication" in text or "authenticat" in text

    def test_an_unreachable_probe_means_go(self) -> None:
        """Getting this backwards silently stops every categorisation."""
        text = " ".join(devdocs.GPU_PROBE["rules"]).lower()
        assert "unreachable" in text and "go, not stop" in text


class TestItIsServed:
    def test_the_index_needs_no_credentials(self, client) -> None:
        """An assistant reads this before there is an account to use."""
        r = client.get("/api/docs")
        assert r.status_code == 200
        assert r.json()["topics"]

    def test_a_topic_is_served_whole(self, client) -> None:
        body = client.get("/api/docs/wake-contract").json()
        assert body["endpoints"]
        assert body["minimal_example"].startswith("from flask")

    def test_an_unknown_topic_lists_the_real_ones(self, client) -> None:
        r = client.get("/api/docs/made-up")
        assert r.status_code == 404
        assert "wake-contract" in r.text


class TestItIsOfferedAsATool:
    def test_the_admin_toolset_includes_it(self) -> None:
        from app.mcp import admin_tools

        src = inspect.getsource(admin_tools)
        assert "admin_developer_docs" in src

    def test_nothing_here_leaks_an_install(self) -> None:
        """Served unauthenticated, so it must be the same everywhere."""
        import json

        text = json.dumps(devdocs.TOPICS)
        for leak in ("192.168.", "duckdns", "neil", "Bearer sk-"):
            assert leak not in text, leak
