"""The settings portal is served, and holds no secrets of its own.

It is deliberately static files and an API, not a template rendered
with data: nothing about this instance is baked into what gets served,
so the page is identical on every install and everything it shows is
fetched with the owner's own token.
"""
from __future__ import annotations

import pathlib

from app.main import app

WEB = pathlib.Path(__file__).resolve().parents[1] / "app" / "web"


class TestItIsServed:
    def test_the_page_loads_without_signing_in(self, client) -> None:
        """It has to: signing in is one of the things it is for."""
        r = client.get("/admin/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]

    def test_its_stylesheet_and_script_are_served(self, client) -> None:
        for path in ("/admin/admin.css", "/admin/admin.js"):
            assert client.get(path).status_code == 200, path

    def test_the_files_are_in_the_package(self) -> None:
        """So they are copied into the image, not just present locally."""
        for name in ("index.html", "admin.css", "admin.js"):
            assert (WEB / name).is_file(), name


class TestThePageCarriesNoSecrets:
    def test_nothing_about_this_instance_is_baked_in(self, client) -> None:
        body = client.get("/admin/").text.lower()
        for leak in ("secret", "password=", "client_id=", "bearer ", "duckdns"):
            assert leak not in body, f"{leak!r} should not be in the served page"

    def test_it_does_not_ask_search_engines_to_index_it(self, client) -> None:
        assert "noindex" in client.get("/admin/").text


class TestItDefersToTheApi:
    def test_the_script_holds_no_rules_of_its_own(self) -> None:
        """Anything decided here would have to be decided twice.

        The same instance will be driven over MCP later, so every rule
        -- who may do what, which settings exist, what is valid -- lives
        in the API. The page renders answers; it does not produce them.
        """
        js = (WEB / "admin.js").read_text()
        assert "/admin/state" in js
        assert "/admin/settings" in js
        assert "/admin/users" in js
        # No allow-list of its own: the API says what is editable.
        assert "EDITABLE" not in js

    def test_an_untouched_secret_field_is_not_submitted(self) -> None:
        """An empty box means "leave it", not "erase it".

        Secrets are never sent to the browser, so every secret field
        starts blank. Submitting that blank would wipe a working Plaid
        secret the first time someone changed an unrelated setting.
        """
        js = (WEB / "admin.js").read_text()
        assert 'el.dataset.kind === "secret" && el.value === ""' in js
